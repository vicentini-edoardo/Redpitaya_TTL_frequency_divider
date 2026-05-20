`timescale 1ns / 1ps
// pulse_gen - Unified NCO-based pulse/harmonic generator with reciprocal frequency counting.
//
// Clock domain: fclk_clk0 (actual measured: 124,999,999 Hz).
//   All control inputs and status outputs share this clock with
//   axi4lite_pulse_regs - no CDC synchronizers needed.
//
// Operation (two phases):
//
//   MEASURE phase (!freerun_active):
//     Incoming trigger edges (both rising and falling) are counted over a fixed
//     time window selected via meas_time_us.
//     period_avg = window_cycles * 2 / edge_count (averaging both edges).
//     After first complete window, period_stable asserts.
//     An iterative divider (49 cycles) computes phase_step_base = 2^48 / period_avg.
//     Once period_stable and division complete, transitions to FREERUN.
//
//   FREERUN phase (freerun_active):
//     48-bit NCO: phase_acc += phase_step each clock.
//     phase_step = (harmonic_mode ? mult_n : 1) * phase_step_base + phase_step_offset.
//
//     Pulse mode (harmonic_mode = 0):
//       Carry-out of phase_acc triggers a pulse of width_n clock cycles.
//       f_out ≈ f_in + phase_step_offset * f_clk / 2^48
//       Duty cycle = width_n / period_avg.
//
//     Harmonic mode (harmonic_mode = 1):
//       Output is phase_acc[47] (MSB) — exact 50% duty square wave.
//       mult_n = width_n[2:0] clamped to [1..5].
//       f_out = mult_n * f_in + phase_step_offset * f_clk / 2^48
//
//   Resetting:
//     soft_reset or deasserting enable clears freerun_active, resets the NCO,
//     and restarts the MEASURE phase from scratch.

module pulse_gen
(
  input  logic        clk,
  input  logic        rstn,

  input  logic        trig_in,

  input  logic        enable,
  input  logic        soft_reset,
  input  logic        harmonic_mode,
  input  logic [31:0] width_n,         // pulse_width cycles (pulse) or mult_n[2:0] (harmonic)
  input  logic [31:0] meas_time_us,

  input  logic signed [47:0] phase_step_offset,

  output logic        trig_rise_dbg,

  output logic        busy,
  output logic        pulse_out,

  output logic [31:0] period_cycles,
  output logic [31:0] period_avg_cycles,
  output logic        period_valid,
  output logic        period_stable,
  output logic        timeout_flag,

  output logic        freerun_active,
  output logic signed [47:0] phase_step_base,
  output logic signed [47:0] phase_step
);

  // Actual measured clock: 124,999,999 Hz.
  // floor(124,999,999 / 1,000,000) = 124 cycles/us — but to keep window timing accurate
  // we use 125 cycles/us (same as before) since the 1 Hz difference is negligible per us.
  // The NCO and output frequency are correct because phase_step_base = 2^48 / period_avg
  // uses the measured period directly — no hardcoded clock assumption in the NCO path.
  localparam logic [31:0] CLK_HZ                = 32'd124_999_999;
  localparam logic [31:0] CLK_PER_US            = 32'd125;
  localparam logic [31:0] PERIOD_TIMEOUT_CYCLES = CLK_HZ;
  localparam logic [31:0] MIN_PERIOD_CYCLES     = 32'd200;

  // Measurement window in clock cycles: meas_time_us * CLK_PER_US.
  // Minimum enforced at 1 ms (125,000 cycles) to avoid division issues.
  logic [31:0] window_cycles;
  always_comb begin
    window_cycles = (meas_time_us >= 32'd1_000) ? meas_time_us * CLK_PER_US : 32'd125_000;
  end

  // ----------------------------------------------------------------
  // 2-FF synchronizer for trig_in, detect both edges
  // ----------------------------------------------------------------
  logic trig_meta, trig_sync, trig_sync_d, trig_rise, trig_fall, trig_edge;

  always_ff @(posedge clk) begin
    if (!rstn) begin
      trig_meta   <= 1'b0;
      trig_sync   <= 1'b0;
      trig_sync_d <= 1'b0;
    end else begin
      trig_meta   <= trig_in;
      trig_sync   <= trig_meta;
      trig_sync_d <= trig_sync;
    end
  end

  assign trig_rise     = trig_sync & ~trig_sync_d;
  assign trig_fall     = ~trig_sync & trig_sync_d;
  assign trig_edge     = trig_rise | trig_fall;
  assign trig_rise_dbg = trig_rise;

  // ----------------------------------------------------------------
  // Reciprocal frequency counter: count edges over fixed time window
  // ----------------------------------------------------------------
  logic [31:0] clk_cnt;
  logic [31:0] edge_cnt;
  logic        window_active;

  always_ff @(posedge clk) begin
    if (!rstn) begin
      clk_cnt           <= 32'd0;
      edge_cnt          <= 32'd0;
      period_cycles     <= 32'd0;
      period_avg_cycles <= 32'd0;
      period_valid      <= 1'b0;
      period_stable     <= 1'b0;
      timeout_flag      <= 1'b0;
      window_active     <= 1'b0;
    end else begin
      if (soft_reset || !enable) begin
        clk_cnt           <= 32'd0;
        edge_cnt          <= 32'd0;
        period_cycles     <= 32'd0;
        period_avg_cycles <= 32'd0;
        period_valid      <= 1'b0;
        period_stable     <= 1'b0;
        timeout_flag      <= 1'b0;
        window_active     <= 1'b0;
      end else begin
        if (!window_active) begin
          if (trig_edge) begin
            window_active <= 1'b1;
            clk_cnt       <= 32'd0;
            edge_cnt      <= 32'd1;
            timeout_flag  <= 1'b0;
          end
        end else begin
          if (trig_edge) begin
            edge_cnt <= edge_cnt + 32'd1;
          end

          if (clk_cnt >= window_cycles - 1) begin
            if (edge_cnt >= 4) begin
              period_cycles     <= edge_cnt;
              period_valid      <= 1'b1;
              period_stable     <= 1'b1;
              period_avg_cycles <= window_cycles / (edge_cnt >> 1);
              timeout_flag      <= 1'b0;
            end else begin
              period_valid  <= 1'b0;
              period_stable <= 1'b0;
              timeout_flag  <= 1'b1;
            end
            window_active <= 1'b0;
            clk_cnt       <= 32'd0;
            edge_cnt      <= 32'd0;
          end else begin
            clk_cnt <= clk_cnt + 32'd1;
          end
        end
      end
    end
  end

  // ----------------------------------------------------------------
  // Iterative divider: phase_step_base = 2^48 / period_avg_cycles
  //
  // Processes the 49-bit dividend (2^48) MSB-first: bit 48 = 1, bits 47:0 = 0.
  // One quotient bit resolved per clock; completes in 49 cycles.
  // ----------------------------------------------------------------
  logic [5:0]  div_step;
  logic [31:0] div_rem;
  logic [47:0] div_quot;
  logic        div_active;
  logic [31:0] div_divisor;
  logic        div_base_valid;
  logic        period_valid_d;

  logic        div_d_bit;
  logic [32:0] div_new_rem;
  logic [32:0] div_sub;

  assign div_d_bit   = div_active && (div_step == 6'd0);
  assign div_new_rem = {div_rem, div_d_bit};
  assign div_sub     = div_new_rem - {1'b0, div_divisor};

  always_ff @(posedge clk) begin
    if (!rstn || soft_reset || !enable) begin
      div_step        <= 6'd0;
      div_rem         <= 32'd0;
      div_quot        <= 48'd0;
      div_active      <= 1'b0;
      div_divisor     <= 32'd1;
      div_base_valid  <= 1'b0;
      period_valid_d  <= 1'b0;
      phase_step_base <= 48'sd0;
    end else begin
      period_valid_d <= period_valid;

      if (period_valid && !period_valid_d && !div_active) begin
        div_active  <= 1'b1;
        div_step    <= 6'd0;
        div_rem     <= 32'd0;
        div_quot    <= 48'd0;
        div_divisor <= (period_avg_cycles != 32'd0) ? period_avg_cycles : 32'd1;
      end else if (div_active) begin
        if (!div_sub[32]) begin
          div_rem  <= div_sub[31:0];
          if (div_step == 6'd48) begin
            phase_step_base <= $signed({div_quot[46:0], 1'b1});
            div_active      <= 1'b0;
            div_base_valid  <= 1'b1;
          end else begin
            div_quot <= {div_quot[46:0], 1'b1};
            div_step <= div_step + 6'd1;
          end
        end else begin
          div_rem  <= div_new_rem[31:0];
          if (div_step == 6'd48) begin
            phase_step_base <= $signed({div_quot[46:0], 1'b0});
            div_active      <= 1'b0;
            div_base_valid  <= 1'b1;
          end else begin
            div_quot <= {div_quot[46:0], 1'b0};
            div_step <= div_step + 6'd1;
          end
        end
      end
    end
  end

  // ----------------------------------------------------------------
  // NCO phase step
  //
  // Pulse mode:    phase_step = phase_step_base + phase_step_offset
  // Harmonic mode: phase_step = mult_n * phase_step_base + phase_step_offset
  //   mult_n = width_n[2:0] clamped to [1..5]
  // ----------------------------------------------------------------
  logic [2:0] mult_n_raw;
  logic [2:0] mult_n_safe;
  assign mult_n_raw  = width_n[2:0];
  assign mult_n_safe = (mult_n_raw == 3'd0) ? 3'd1 :
                       (mult_n_raw >  3'd5) ? 3'd5 : mult_n_raw;

  logic [50:0] mult_step;
  assign mult_step  = harmonic_mode ?
      (51'(phase_step_base[47:0])) * (51'(mult_n_safe)) :
      51'(phase_step_base[47:0]);
  assign phase_step = $signed(mult_step[47:0]) + phase_step_offset;

  logic [47:0] phase_acc;
  logic [48:0] acc_sum;

  assign acc_sum = {1'b0, phase_acc} + {1'b0, phase_step[47:0]};

  // ----------------------------------------------------------------
  // Freerun state + NCO accumulator
  // ----------------------------------------------------------------
  always_ff @(posedge clk) begin
    if (!rstn) begin
      freerun_active <= 1'b0;
      phase_acc      <= 48'd0;
    end else begin
      if (soft_reset || !enable) begin
        freerun_active <= 1'b0;
        phase_acc      <= 48'd0;
      end else if (!freerun_active) begin
        if (period_stable && div_base_valid && !div_active)
          freerun_active <= 1'b1;
      end else begin
        phase_acc <= acc_sum[47:0];
      end
    end
  end

  // ----------------------------------------------------------------
  // Output generation
  //
  // Harmonic mode: phase_acc[47] gives exact 50% duty square wave.
  // Pulse mode:    NCO carry-out (acc_sum[48]) triggers width_n-cycle pulse.
  // ----------------------------------------------------------------
  logic [31:0] width_cnt;
  logic        nco_tick;

  assign nco_tick = acc_sum[48] & freerun_active;
  assign busy     = freerun_active;

  always_ff @(posedge clk) begin
    if (!rstn || soft_reset || !enable || !freerun_active) begin
      pulse_out <= 1'b0;
      width_cnt <= 32'd0;
    end else if (harmonic_mode) begin
      pulse_out <= phase_acc[47];
      width_cnt <= 32'd0;
    end else begin
      // Pulse mode: width counter on NCO carry-out
      if (nco_tick) begin
        if (width_n != 32'd0) begin
          pulse_out <= 1'b1;
          width_cnt <= width_n - 32'd1;
        end else begin
          pulse_out <= 1'b0;
        end
      end else if (pulse_out) begin
        if (width_cnt == 32'd0)
          pulse_out <= 1'b0;
        else
          width_cnt <= width_cnt - 32'd1;
      end
    end
  end

endmodule

`timescale 1ns / 1ps
// pulse_gen - NCO-based pulse generator referenced to an external trigger.
//
// Clock domain: fclk_clk0 (125 MHz).
//   All control inputs and status outputs share this clock with
//   axi4lite_pulse_regs - no CDC synchronizers needed.
//
// Operation (two phases):
//
//   MEASURE phase (!freerun_active):
//     Incoming trigger edges are timed by a free-running counter.
//     A first-order IIR (alpha = 1/8) smooths the measured period.
//     After STABLE_COUNT (8) consecutive valid periods, period_stable asserts.
//     An iterative divider (49 cycles) computes phase_step_base = 2^48 / period_avg.
//     Once period_stable and the first division have completed, transitions to FREERUN.
//
//   FREERUN phase (freerun_active):
//     48-bit NCO: phase_acc += phase_step each clock.
//     phase_step = phase_step_base + phase_step_offset (live; changes take effect immediately).
//     Carry-out of bit 47 triggers a new output pulse of pulse_width cycles.
//     phase_step_base is recomputed whenever a new valid period measurement arrives.
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
  input  logic [31:0] pulse_width,

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

  localparam logic [31:0] PERIOD_TIMEOUT_CYCLES = 32'd125_000_000;
  localparam logic [31:0] MIN_PERIOD_CYCLES     = 32'd200;
  localparam int unsigned AVG_SHIFT             = 3;
  localparam int unsigned STABLE_COUNT          = 8;

  // ----------------------------------------------------------------
  // 2-FF synchronizer for trig_in
  // ----------------------------------------------------------------
  logic trig_meta, trig_sync, trig_sync_d, trig_rise;

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
  assign trig_rise_dbg = trig_rise;

  // ----------------------------------------------------------------
  // IIR average - combinational
  // ----------------------------------------------------------------
  logic [31:0] period_cnt;
  logic signed [32:0] avg_error_c, avg_step_c, avg_ext_c;
  logic [31:0] period_avg_next;

  always_comb begin
    avg_error_c     = $signed({1'b0, period_cnt}) - $signed({1'b0, period_avg_cycles});
    avg_step_c      = avg_error_c >>> AVG_SHIFT;
    avg_ext_c       = $signed({1'b0, period_avg_cycles}) + avg_step_c;
    period_avg_next = (avg_ext_c < 0) ? 32'd0 : avg_ext_c[31:0];
  end

  // ----------------------------------------------------------------
  // Period measurement + IIR + warm-up counter
  // ----------------------------------------------------------------
  logic        seen_first_trigger;
  logic [3:0]  stable_cnt;

  always_ff @(posedge clk) begin
    if (!rstn) begin
      period_cnt         <= 32'd0;
      period_cycles      <= 32'd0;
      period_avg_cycles  <= 32'd0;
      period_valid       <= 1'b0;
      period_stable      <= 1'b0;
      stable_cnt         <= 4'd0;
      timeout_flag       <= 1'b0;
      seen_first_trigger <= 1'b0;
    end else begin
      if (soft_reset || !enable) begin
        period_cnt         <= 32'd0;
        period_cycles      <= 32'd0;
        period_avg_cycles  <= 32'd0;
        period_valid       <= 1'b0;
        period_stable      <= 1'b0;
        stable_cnt         <= 4'd0;
        timeout_flag       <= 1'b0;
        seen_first_trigger <= 1'b0;
      end else begin
        if (trig_rise) begin
          if (!seen_first_trigger) begin
            seen_first_trigger <= 1'b1;
            period_cnt         <= 32'd0;
            timeout_flag       <= 1'b0;
          end else begin
            if (period_cnt >= MIN_PERIOD_CYCLES) begin
              period_cycles     <= period_cnt;
              timeout_flag      <= 1'b0;
              period_valid      <= 1'b1;
              period_avg_cycles <= period_valid ? period_avg_next : period_cnt;
              if (!period_stable) begin
                if (stable_cnt == 4'(STABLE_COUNT - 1))
                  period_stable <= 1'b1;
                else
                  stable_cnt <= stable_cnt + 4'd1;
              end
              period_cnt <= 32'd0;
            end
          end
        end else begin
          if (seen_first_trigger) begin
            if (period_cnt != 32'hFFFF_FFFF)
              period_cnt <= period_cnt + 32'd1;
            if (period_cnt >= PERIOD_TIMEOUT_CYCLES) begin
              period_valid  <= 1'b0;
              period_stable <= 1'b0;
              stable_cnt    <= 4'd0;
              timeout_flag  <= 1'b1;
            end
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
  // Invariant: 0 <= div_rem < div_divisor throughout.
  //
  // A new division is triggered on every rising edge of period_valid,
  // provided the previous one has finished (min period >= 200 >> 49 cycles).
  // ----------------------------------------------------------------
  logic [5:0]  div_step;
  logic [31:0] div_rem;
  logic [47:0] div_quot;
  logic        div_active;
  logic [31:0] div_divisor;
  logic        div_base_valid;
  logic        period_valid_d;

  // Combinational: current dividend bit and remainder/subtraction
  logic        div_d_bit;
  logic [32:0] div_new_rem;
  logic [32:0] div_sub;

  assign div_d_bit   = div_active && (div_step == 6'd0);
  assign div_new_rem = {div_rem, div_d_bit};
  assign div_sub     = div_new_rem - {1'b0, div_divisor};

  always_ff @(posedge clk) begin
    if (!rstn || soft_reset || !enable) begin
      div_step       <= 6'd0;
      div_rem        <= 32'd0;
      div_quot       <= 48'd0;
      div_active     <= 1'b0;
      div_divisor    <= 32'd1;
      div_base_valid <= 1'b0;
      period_valid_d <= 1'b0;
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
          // div_new_rem >= divisor: subtract, quotient bit = 1
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
          // div_new_rem < divisor: keep remainder, quotient bit = 0
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
  // NCO: phase_step = phase_step_base + phase_step_offset (combinational)
  // ----------------------------------------------------------------
  assign phase_step = phase_step_base + phase_step_offset;

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
  // Pulse generation on NCO carry-out
  // ----------------------------------------------------------------
  logic [31:0] width_cnt;
  logic        nco_tick;

  assign nco_tick = acc_sum[48] & freerun_active;
  assign busy     = freerun_active & pulse_out;

  always_ff @(posedge clk) begin
    if (!rstn || soft_reset || !enable || !freerun_active) begin
      pulse_out <= 1'b0;
      width_cnt <= 32'd0;
    end else begin
      if (nco_tick) begin
        if (pulse_width != 32'd0) begin
          pulse_out <= 1'b1;
          width_cnt <= pulse_width - 32'd1;
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

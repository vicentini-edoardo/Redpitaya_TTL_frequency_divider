`timescale 1ns / 1ps
// pulse_gen - NCO-based pulse generator with reciprocal frequency counting.
//
// Clock domain: fclk_clk0 (125 MHz).
//   All control inputs and status outputs share this clock with
//   axi4lite_pulse_regs - no CDC synchronizers needed.
//
// Operation (two phases):
//
//   MEASURE phase (!freerun_active):
//     Incoming trigger edges (both rising and falling) are counted over a fixed
//     time window (10 ms, 100 ms, 500 ms, or 1000 ms selected via reg_window).
//     period_avg = window_cycles * 2 / edge_count (averaging both edges)
//     After first complete window, period_stable asserts.
//     An iterative divider (49 cycles) computes phase_step_base = 2^48 / period_avg.
//     Once period_stable and division complete, transitions to FREERUN.
//
//   FREERUN phase (freerun_active):
//     48-bit NCO: phase_acc += phase_step each clock.
//     phase_step = phase_step_base + phase_step_offset (live; changes take effect immediately).
//     Carry-out of bit 47 triggers a new output pulse of pulse_width cycles.
//     phase_step_base is recomputed after each measurement window completes.
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
  input  logic [ 1:0] window_select,

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

  // Window sizes in clock cycles (125 MHz clock)
  // 0: 10 ms   = 1,250,000 cycles
  // 1: 100 ms  = 12,500,000 cycles
  // 2: 500 ms  = 62,500,000 cycles
  // 3: 1000 ms = 125,000,000 cycles
  logic [31:0] window_cycles;
  always_comb begin
    case (window_select)
      2'd0:    window_cycles = 32'd1_250_000;
      2'd1:    window_cycles = 32'd12_500_000;
      2'd2:    window_cycles = 32'd62_500_000;
      default: window_cycles = 32'd125_000_000;
    endcase
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
      clk_cnt         <= 32'd0;
      edge_cnt        <= 32'd0;
      period_cycles   <= 32'd0;
      period_avg_cycles <= 32'd0;
      period_valid    <= 1'b0;
      period_stable   <= 1'b0;
      timeout_flag    <= 1'b0;
      window_active   <= 1'b0;
    end else begin
      if (soft_reset || !enable) begin
        clk_cnt         <= 32'd0;
        edge_cnt        <= 32'd0;
        period_cycles   <= 32'd0;
        period_avg_cycles <= 32'd0;
        period_valid    <= 1'b0;
        period_stable   <= 1'b0;
        timeout_flag    <= 1'b0;
        window_active   <= 1'b0;
      end else begin
        if (!window_active) begin
          // Start first window when first edge detected
          if (trig_edge) begin
            window_active <= 1'b1;
            clk_cnt       <= 32'd0;
            edge_cnt      <= 32'd1;
            timeout_flag  <= 1'b0;
          end
        end else begin
          // Count edges and clock cycles until window complete
          if (trig_edge) begin
            edge_cnt <= edge_cnt + 32'd1;
          end

          if (clk_cnt >= window_cycles - 1) begin
            // Window complete: compute period_avg = window_cycles / (edge_cnt / 2)
            // = 2 * window_cycles / edge_cnt (accounts for both edges)
            // Avoid divide by zero
            if (edge_cnt >= 4) begin
              period_cycles     <= edge_cnt;
              period_valid      <= 1'b1;
              period_stable     <= 1'b1;  // Stable immediately with long averaging
              period_avg_cycles <= window_cycles / (edge_cnt >> 1);
              timeout_flag      <= 1'b0;
            end else begin
              period_valid      <= 1'b0;
              period_stable     <= 1'b0;
              timeout_flag      <= 1'b1;
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

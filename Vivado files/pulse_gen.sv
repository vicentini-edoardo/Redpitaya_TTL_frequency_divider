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
//     True reciprocal counting: rising trigger edges are counted over a time
//     window selected via meas_time_us, and the elapsed clock cycles between
//     the FIRST and LAST rising edge (meas_span) are latched. After a valid
//     window, period_stable asserts and an iterative divider (48 cycles)
//     computes:
//       phase_step_base = 2^48 * (edge_count - 1) / meas_span
//     edge_count - 1 is the number of whole input periods inside meas_span,
//     so the quantization error is ±1 clock at each end of the span
//     (~2 / meas_span relative) instead of ±1 edge per window
//     (f_clk / (2 * window_cycles) Hz) of the previous fixed-window scheme.
//     Using rising edges only also makes the result independent of the input
//     duty cycle, and dead time after the last edge no longer biases the
//     estimate. The 2-FF synchronizer latency cancels because both ends of
//     the span pass through the same path.
//     Once period_stable and division complete, transitions to FREERUN.
//     Measurement keeps running in FREERUN; phase_step_base refreshes every
//     window so the output tracks slow input drift.
//
//   FREERUN phase (freerun_active):
//     48-bit NCO: phase_acc += phase_step each clock.
//     phase_step = (harmonic_mode ? mult_n : 1) * phase_step_base + phase_step_offset.
//
//     Pulse mode (harmonic_mode = 0):
//       Carry-out of phase_acc triggers a pulse of width_n clock cycles.
//       f_out ≈ f_in + phase_step_offset * f_clk / 2^48
//       Duty cycle = width_n / (2^48 / phase_step_base).
//
//     Harmonic mode (harmonic_mode = 1):
//       Output is phase_acc[47] (MSB) — exact 50% duty square wave.
//       mult_n = width_n[2:0] clamped to [1..5].
//       f_out = mult_n * f_in + phase_step_offset * f_clk / 2^48
//
//     Edge-locked option (edge_lock = 1, pulse or harmonic mode):
//       Same NCO, but the phase is re-anchored to the input on every
//       accepted rising edge (see anchor gating below): a target register
//       integrates only phase_step_offset, and phase_acc snaps to it at
//       each anchor edge. The output-to-input frequency shift is then
//       exactly f_shift with the beat phase coherent indefinitely —
//       measurement error no longer integrates into phase drift. Cost:
//       each output pulse inherits the timing of one input edge (source
//       jitter + one-clock synchronizer quantization).
//
//     Oscillating delay mode (osc_mode = 1, pulse mode only):
//       The sign of phase_step_offset alternates every osc_half_period
//       ticks, so the output pulse delay relative to the input sweeps a
//       triangle wave P0-P → P0+P → P0-P at rate f_shift / (4·P).
//       A separate register (osc_target) integrates only the ±offset part
//       of the step; on every synchronized input rising edge phase_acc is
//       re-anchored to osc_target. Each pulse therefore fires
//       (2^48 - osc_target) / phase_step_base clock ticks after the
//       physical input edge: the reciprocal-counter quantization error
//       (up to f_clk / (2·window_cycles) Hz) cannot accumulate into a
//       phase drift, and P0 / P are true phases referenced to the input
//       edge instead of the arbitrary instant freerun started.
//       (The 2-FF trigger synchronizer plus output register add a fixed
//       ~4-clock latency — a constant, calibratable delay offset.)
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
  input  logic        osc_mode,
  input  logic        edge_lock,       // anchor NCO phase to input edges (pulse/harmonic)
  input  logic [31:0] width_n,         // pulse_width cycles (pulse) or mult_n[2:0] (harmonic)
  input  logic [31:0] meas_time_us,

  input  logic signed [47:0] phase_step_offset,

  input  logic [31:0] osc_half_period,    // clock ticks per half-oscillation
  input  logic [47:0] osc_phase_preload,  // accumulator preload for delay = P0 - P

  input  logic [47:0] trig_phase_step,   // DIO2 free-running square wave: 48-bit NCO step (0=off)

  output logic        trig_rise_dbg,

  output logic        busy,
  output logic        pulse_out,
  output logic        trig_out,

  output logic [31:0] meas_span,       // cycles between first/last rising edge (reported at 0x14)
  output logic [31:0] edge_cnt_out,    // rising-edge count from last window (reported at 0x18)
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
  // The NCO frequency is correct because phase_step_base derives directly from
  // completed edge intervals and the measurement window in clock cycles. There is
  // no hardcoded clock assumption in the NCO path.
  localparam logic [31:0] CLK_HZ                = 32'd124_999_999;
  localparam logic [31:0] CLK_PER_US            = 32'd125;

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
  // Anchor-edge gating (edge_lock / osc modes).
  //
  // A rising edge is accepted as a phase anchor only if at least 3/4 of
  // the estimated input period has elapsed since the last accepted edge.
  // This rejects ringing / double-trigger glitches, which would otherwise
  // snap the NCO to a mid-period value and cause a hard output phase
  // jump. Late edges are always accepted (after a coast the target is
  // still valid at any true edge, since the base part of the step
  // advances an integer number of wraps per input period).
  //
  // The period estimate is taken from the gap between accepted edges,
  // updated only when the new gap is plausible (<= 1.5x the previous
  // estimate) so a missed edge does not corrupt it, and re-acquired from
  // scratch if the input pauses for more than 4 estimated periods.
  // ----------------------------------------------------------------
  logic [31:0] anchor_gap;         // clocks since last accepted anchor edge
  logic [31:0] anchor_period_est;  // estimated input period in clocks
  logic        anchor_have_prev;
  logic        anchor_rise;

  assign anchor_rise = trig_rise &&
      ((anchor_period_est == 32'd0) ||
       (anchor_gap >= anchor_period_est - (anchor_period_est >> 2)));

  always_ff @(posedge clk) begin
    if (!rstn || soft_reset || !enable) begin
      anchor_gap        <= 32'd0;
      anchor_period_est <= 32'd0;
      anchor_have_prev  <= 1'b0;
    end else if (anchor_rise) begin
      if (anchor_have_prev &&
          (anchor_period_est == 32'd0 ||
           anchor_gap <= anchor_period_est + (anchor_period_est >> 1)))
        anchor_period_est <= anchor_gap;
      anchor_have_prev <= 1'b1;
      anchor_gap       <= 32'd0;
    end else begin
      if (anchor_period_est != 32'd0 &&
          anchor_gap > {anchor_period_est[29:0], 2'b00}) begin
        // input paused (or estimate corrupted): re-acquire
        anchor_period_est <= 32'd0;
        anchor_have_prev  <= 1'b0;
      end
      if (anchor_gap != 32'hFFFF_FFFF)
        anchor_gap <= anchor_gap + 32'd1;
    end
  end

  // ----------------------------------------------------------------
  // True reciprocal frequency counter.
  //
  // The window opens on a rising edge; rising edges are counted and the
  // clock count at each one is latched (span_last). At window close the
  // span between the first and last rising edge holds edge_cnt-1 whole
  // input periods regardless of duty cycle, and any dead time after the
  // last edge is excluded from the measurement.
  // ----------------------------------------------------------------
  logic [31:0] clk_cnt;
  logic [31:0] edge_cnt;    // rising edges since the window opened
  logic [31:0] span_last;   // elapsed cycles at the most recent rising edge
  logic        window_active;
  logic        period_sample_strobe;

  always_ff @(posedge clk) begin
    if (!rstn) begin
      clk_cnt       <= 32'd0;
      edge_cnt      <= 32'd0;
      span_last     <= 32'd0;
      meas_span     <= 32'd0;
      edge_cnt_out  <= 32'd0;
      period_valid  <= 1'b0;
      period_stable <= 1'b0;
      timeout_flag  <= 1'b0;
      window_active <= 1'b0;
      period_sample_strobe <= 1'b0;
    end else begin
      period_sample_strobe <= 1'b0;

      if (soft_reset || !enable) begin
        clk_cnt       <= 32'd0;
        edge_cnt      <= 32'd0;
        span_last     <= 32'd0;
        meas_span     <= 32'd0;
        edge_cnt_out  <= 32'd0;
        period_valid  <= 1'b0;
        period_stable <= 1'b0;
        timeout_flag  <= 1'b0;
        window_active <= 1'b0;
        period_sample_strobe <= 1'b0;
      end else begin
        if (!window_active) begin
          if (trig_rise) begin
            window_active <= 1'b1;
            clk_cnt       <= 32'd0;
            edge_cnt      <= 32'd1;
            span_last     <= 32'd0;
            timeout_flag  <= 1'b0;
          end
        end else begin
          if (trig_rise) begin
            edge_cnt  <= edge_cnt + 32'd1;
            span_last <= clk_cnt + 32'd1;   // cycles since the opening edge
          end

          if (clk_cnt >= window_cycles - 1) begin
            // >= 3 rising edges = >= 2 whole periods inside the span
            if (edge_cnt >= 3 && span_last != 32'd0) begin
              meas_span     <= span_last;
              edge_cnt_out  <= edge_cnt;
              period_valid  <= 1'b1;
              period_stable <= 1'b1;
              timeout_flag  <= 1'b0;
              period_sample_strobe <= 1'b1;
            end else begin
              period_valid  <= 1'b0;
              period_stable <= 1'b0;
              timeout_flag  <= 1'b1;
            end
            window_active <= 1'b0;
            clk_cnt       <= 32'd0;
            edge_cnt      <= 32'd0;
            span_last     <= 32'd0;
          end else begin
            clk_cnt <= clk_cnt + 32'd1;
          end
        end
      end
    end
  end

  // ----------------------------------------------------------------
  // Iterative divider:
  //   phase_step_base = 2^48 * (edge_cnt - 1) / meas_span
  //
  // edge_cnt - 1 whole input periods span meas_span clock cycles, so the
  // fraction is computed directly with no intermediate truncation. Since
  // (edge_cnt - 1) / meas_span < 1 (period > 1 clock), we initialise
  // rem = edge_cnt - 1 and shift in 48 zero-bits from the dividend, using
  // meas_span as divisor. This is equivalent to standard long-division of
  // ((edge_cnt - 1) << 48) / meas_span but without needing an 80-bit shift
  // register. 48 steps, one quotient bit per clock.
  // ----------------------------------------------------------------
  logic [5:0]  div_step;
  logic [32:0] div_rem;
  logic [47:0] div_quot;
  logic        div_active;
  logic [32:0] div_divisor;
  logic        div_base_valid;

  logic [33:0] div_new_rem;   // {div_rem, 0} shifted left by 1
  logic [33:0] div_sub;

  assign div_new_rem = {div_rem, 1'b0};   // shift in 0 (dividend bits are all zero)
  assign div_sub     = div_new_rem - {1'b0, div_divisor};

  always_ff @(posedge clk) begin
    if (!rstn || soft_reset || !enable) begin
      div_step        <= 6'd0;
      div_rem         <= 33'd0;
      div_quot        <= 48'd0;
      div_active      <= 1'b0;
      div_divisor     <= 33'd1;
      div_base_valid  <= 1'b0;
      phase_step_base <= 48'sd0;
    end else begin
      if (period_sample_strobe && !div_active) begin
        div_active  <= 1'b1;
        div_step    <= 6'd0;
        // Exclude the edge that opened the window; it is a boundary marker,
        // not a completed period. edge_cnt_out - 1 whole periods elapsed in
        // meas_span cycles.
        div_rem     <= {1'b0, edge_cnt_out - 32'd1};
        div_quot    <= 48'd0;
        div_divisor <= (meas_span != 32'd0) ? {1'b0, meas_span} : 33'd1;
      end else if (div_active) begin
        if (!div_sub[33]) begin
          // rem >= divisor: quotient bit = 1
          div_rem  <= div_sub[32:0];
          if (div_step == 6'd47) begin
            phase_step_base <= $signed({div_quot[46:0], 1'b1});
            div_active      <= 1'b0;
            div_base_valid  <= 1'b1;
          end else begin
            div_quot <= {div_quot[46:0], 1'b1};
            div_step <= div_step + 6'd1;
          end
        end else begin
          // rem < divisor: quotient bit = 0
          div_rem  <= div_new_rem[32:0];
          if (div_step == 6'd47) begin
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
  // Osc mode:      phase_step_offset sign alternates every osc_half_period ticks
  // ----------------------------------------------------------------
  logic [2:0] mult_n_raw;
  logic [2:0] mult_n_safe;
  assign mult_n_raw  = width_n[2:0];
  assign mult_n_safe = (mult_n_raw == 3'd0) ? 3'd1 :
                       (mult_n_raw >  3'd5) ? 3'd5 : mult_n_raw;

  logic [50:0] mult_step;
  assign mult_step = harmonic_mode ?
      (51'(phase_step_base[47:0])) * (51'(mult_n_safe)) :
      51'(phase_step_base[47:0]);

  // osc_sign=0: subtract offset (f_out < f_in → phase delay grows toward P0+P)
  // osc_sign=1: add offset    (f_out > f_in → phase delay shrinks toward P0-P)
  logic osc_sign;
  logic signed [47:0] phase_step_eff;
  assign phase_step_eff = osc_mode ? (osc_sign ? phase_step_offset : -phase_step_offset)
                                   : phase_step_offset;
  assign phase_step = $signed(mult_step[47:0]) + phase_step_eff;

  logic [47:0] phase_acc;
  logic [48:0] acc_sum;

  assign acc_sum = {1'b0, phase_acc} + {1'b0, phase_step[47:0]};

  // ----------------------------------------------------------------
  // Freerun state + NCO accumulator
  //
  // Edge-locked operation (lock_en = osc_mode | edge_lock):
  //   osc_target integrates only the phase_step_offset part of the step
  //   (with alternating sign in osc mode — the triangle sweep). On every
  //   accepted input rising edge (anchor_rise) phase_acc is re-anchored
  //   to osc_target, so the output phase is referenced to the physical
  //   input edge and the measured-vs-actual f_in mismatch is cleared
  //   once per input period instead of accumulating forever. The offset
  //   part is exact by construction: f_out - [N·]f_in = f_shift, with
  //   the beat coherent indefinitely.
  //   Between edges (or if the input stops) the NCO freeruns as before.
  //   This works identically in harmonic mode because the base part of
  //   the step advances exactly mult_n whole wraps per input period.
  //
  // On lock enable the accumulator and target are preloaded and the NCO
  // is held (osc_run = 0) until the first input rising edge, so the
  // phase trajectory starts anchored to the input (osc mode: first pulse
  // at delay P0 - P). Without the hold, the pulses emitted between the
  // (arbitrary) preload instant and the first edge would sit at a random
  // phase.
  //
  // Osc mode additionally alternates the offset sign every
  // osc_half_period ticks, sweeping output phase P0-P → P0+P → P0-P at
  // rate f_shift / (4·P).
  // ----------------------------------------------------------------
  logic        lock_en;
  logic [31:0] osc_counter;
  logic        lock_en_prev;
  logic        osc_mode_prev;
  logic        osc_run;          // 0 = preloaded, waiting for anchoring edge
  logic [47:0] osc_target;
  logic [47:0] osc_target_next;

  assign lock_en         = osc_mode | edge_lock;
  assign osc_target_next = osc_target + phase_step_eff[47:0];   // mod 2^48

  always_ff @(posedge clk) begin
    if (!rstn) begin
      freerun_active <= 1'b0;
      phase_acc      <= 48'd0;
      osc_counter    <= 32'd0;
      osc_sign       <= 1'b0;
      lock_en_prev   <= 1'b0;
      osc_mode_prev  <= 1'b0;
      osc_run        <= 1'b0;
      osc_target     <= 48'd0;
    end else begin
      lock_en_prev  <= lock_en;
      osc_mode_prev <= osc_mode;
      if (soft_reset || !enable) begin
        freerun_active <= 1'b0;
        phase_acc      <= 48'd0;
        osc_counter    <= 32'd0;
        osc_sign       <= 1'b0;
        osc_run        <= 1'b0;
        osc_target     <= 48'd0;
      end else if (!freerun_active) begin
        if (period_stable && div_base_valid && !div_active) begin
          freerun_active <= 1'b1;
          if (lock_en) begin
            phase_acc   <= osc_phase_preload;
            osc_target  <= osc_phase_preload;
            osc_counter <= 32'd0;
            osc_sign    <= 1'b0;
            osc_run     <= 1'b0;
          end
        end
      end else begin
        // freerun active
        if ((lock_en && !lock_en_prev) || (osc_mode && !osc_mode_prev)) begin
          // entering a locked mode (or switching edge_lock → osc):
          // preload accumulator, reset oscillation, re-arm
          phase_acc   <= osc_phase_preload;
          osc_target  <= osc_phase_preload;
          osc_counter <= 32'd0;
          osc_sign    <= 1'b0;
          osc_run     <= 1'b0;
        end else if (lock_en && !osc_run) begin
          // armed: hold the preload until the first accepted input rising
          // edge so the phase trajectory starts anchored to the input
          phase_acc   <= osc_phase_preload;
          osc_target  <= osc_phase_preload;
          osc_counter <= 32'd0;
          osc_sign    <= 1'b0;
          if (anchor_rise)
            osc_run <= 1'b1;
        end else begin
          // Re-anchor to the input on every accepted rising edge;
          // nco_tick still evaluates the pre-snap acc_sum this tick.
          phase_acc <= (lock_en && anchor_rise) ? osc_target_next
                                                : acc_sum[47:0];
          if (lock_en) begin
            osc_target <= osc_target_next;
            if (osc_mode) begin
              if (osc_half_period > 32'd0 &&
                  osc_counter >= osc_half_period - 32'd1) begin
                osc_counter <= 32'd0;
                osc_sign    <= ~osc_sign;
              end else begin
                osc_counter <= osc_counter + 32'd1;
              end
            end else begin
              // plain edge_lock: constant offset sign, no triangle
              osc_counter <= 32'd0;
              osc_sign    <= 1'b0;
            end
          end else begin
            osc_counter <= 32'd0;
            osc_sign    <= 1'b0;
            osc_run     <= 1'b0;
            osc_target  <= 48'd0;
          end
        end
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

  // In locked modes no pulses are emitted until the first input edge has
  // anchored the phase trajectory (osc_run).
  assign nco_tick = acc_sum[48] & freerun_active & (~lock_en | osc_run);
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

  // ----------------------------------------------------------------
  // DIO2 free-running square wave (independent of DIO1 NCO / enable)
  //
  // f_DIO2 = trig_phase_step * CLK_HZ / 2^48.
  // 0 → output held low (disabled).
  // ----------------------------------------------------------------
  logic [47:0] trig_phase_acc;

  always_ff @(posedge clk) begin
    if (!rstn) begin
      trig_phase_acc <= 48'd0;
      trig_out       <= 1'b0;
    end else if (trig_phase_step == 48'd0) begin
      trig_phase_acc <= 48'd0;
      trig_out       <= 1'b0;
    end else begin
      trig_phase_acc <= trig_phase_acc + trig_phase_step;
      trig_out       <= trig_phase_acc[47];
    end
  end

endmodule

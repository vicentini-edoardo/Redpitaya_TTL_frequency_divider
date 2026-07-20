`timescale 1ns / 1ps
// axi4lite_pulse_regs - AXI4-Lite slave register file for the unified pulse/harmonic gen.
//
// Clock domain: fclk_clk0 (PS GP0 master clock).
//               All register outputs drive pulse_gen on the same clock - no CDC needed.
//
// Register map (byte offset / 32-bit word):
//   0x00 RW  control:  [0] enable, [1] soft_reset (self-clearing, reads 0),
//                      [2] force_high (output forced to 1), [3] harmonic_mode,
//                      [4] osc_mode (stepped strobe scan),
//                      [5] edge_lock (anchor NCO phase to input edges)
//   0x04 RW  trig_phase_step_lo: bits [31:0] of DIO2 48-bit NCO step (0=off)
//   0x08 RW  width_n:  pulse width in clock cycles (pulse mode) OR
//                      harmonic multiplier 1..5 in bits [2:0] (harmonic mode)
//   0x0C RW  trig_phase_step_hi: bits [47:32] of DIO2 48-bit NCO step
//   0x10 RO  status:   [0] busy, [1] period_valid, [2] period_stable, [3] timeout,
//                      [4] freerun_active, [5] strobe_done
//                      (bit order matches the rdata concat below: see 5'd4 read)
//   0x14 RO  meas_span       (clock cycles between first and last rising edge of last window)
//   0x18 RO  edge_cnt_out    (rising-edge count from last window; f_in = CLK_HZ * (val-1) / meas_span)
//   0x1C RW  phase_step_offset_lo   bits [31:0]  of signed 48-bit NCO offset
//   0x20 RW  phase_step_offset_hi   bits [47:32] of signed 48-bit NCO offset (in [15:0])
//   0x24 RO  phase_step_base_lo     bits [31:0]  of computed base step
//   0x28 RO  phase_step_base_hi     bits [47:32] of computed base step (in [15:0])
//   0x2C RO  phase_step_lo          bits [31:0]  of live phase_step
//   0x30 RO  phase_step_hi          bits [47:32] of live phase_step (in [15:0])
//   0x34 RW  meas_time_us: [31:0] measurement window duration in microseconds
//   0x38 RW  dwell_cycles: [31:0] clock ticks per strobe point (osc mode)
//   0x3C RW  osc_phase_preload_lo: bits [31:0]  of 48-bit accumulator preload
//   0x40 RW  osc_phase_preload_hi: bits [47:32] of 48-bit accumulator preload (in [15:0])
//   0x44 RW  n_steps:    [31:0] strobe points per scan (osc mode, >=1)
//   0x48 RO  step_index: [31:0] current strobe point (0-based)
//
//   In osc mode phase_step_offset (0x1C/0x20) is repurposed as the per-step
//   target increment: two's-complement of round(step_frac * 2^48).
//
// AXI4-Lite write handshake:
//   aw_seen / w_seen track independent acceptance of AW and W channels.
//   Both must arrive before the write is committed and BVALID is asserted.
//
// Notes:
//   soft_reset (control[1]) is self-clearing - it asserts pulse_soft_reset for one cycle.
//   force_high (control[2]) overrides the output pin HIGH regardless of NCO state.
//   harmonic_mode (control[3]) selects output: NCO carry-out (0) vs phase_acc[47] (1).
//   Writes to read-only or undefined addresses are silently accepted (BRESP=OKAY).
//   48-bit values (trig_phase_step, phase_step_offset, osc_phase_preload) are
//   staged in hi/lo shadow registers and COMMITTED to pulse_gen one cycle after
//   the LO word is written (write order: hi first, then lo — matching rp_ctl.c).
//   pulse_gen therefore never sees a torn {new_hi, old_lo} value while running.

module axi4lite_pulse_regs
(
  input  logic        clk,
  input  logic        rstn,

  // AXI4-Lite slave
  input  logic [31:0] s_axi_awaddr,
  input  logic        s_axi_awvalid,
  output logic        s_axi_awready,

  input  logic [31:0] s_axi_wdata,
  input  logic [ 3:0] s_axi_wstrb,
  input  logic        s_axi_wvalid,
  output logic        s_axi_wready,

  output logic [ 1:0] s_axi_bresp,
  output logic        s_axi_bvalid,
  input  logic        s_axi_bready,

  input  logic [31:0] s_axi_araddr,
  input  logic        s_axi_arvalid,
  output logic        s_axi_arready,

  output logic [31:0] s_axi_rdata,
  output logic [ 1:0] s_axi_rresp,
  output logic        s_axi_rvalid,
  input  logic        s_axi_rready,

  // Control outputs -> pulse_gen
  output logic        pulse_enable,
  output logic        pulse_soft_reset,
  output logic        force_high,
  output logic        harmonic_mode,
  output logic        osc_mode,
  output logic        edge_lock,
  output logic [47:0] trig_phase_step,   // DIO2 48-bit NCO step (0=off)
  output logic [31:0] width_n,           // pulse_width (pulse mode) or mult_n[2:0] (harmonic mode)
  output logic [31:0] meas_time_us,
  output logic signed [47:0] phase_step_offset,
  output logic [31:0] dwell_cycles,
  output logic [47:0] osc_phase_preload,
  output logic [31:0] n_steps,

  // Status inputs <- pulse_gen
  input  logic        pulse_busy,
  input  logic [31:0] meas_span,
  input  logic [31:0] edge_cnt_out,
  input  logic        period_valid,
  input  logic        period_stable,
  input  logic        timeout_flag,
  input  logic        freerun_active,
  input  logic signed [47:0] phase_step_base,
  input  logic signed [47:0] phase_step,
  input  logic [31:0] step_index,
  input  logic        strobe_done
);

  logic [31:0] reg_control;
  logic [31:0] reg_trig_phase_step_lo;
  logic [31:0] reg_width_n;
  logic [15:0] reg_trig_phase_step_hi;
  logic [31:0] reg_meas_time_us;
  logic [31:0] reg_phase_step_offset_lo;
  logic [15:0] reg_phase_step_offset_hi;
  logic [31:0] reg_dwell_cycles;
  logic [31:0] reg_osc_phase_preload_lo;
  logic [15:0] reg_osc_phase_preload_hi;
  logic [31:0] reg_n_steps;

  // Committed 48-bit values presented to pulse_gen (updated atomically one
  // cycle after the LO word of the pair is written).
  logic [47:0] trig_phase_step_q;
  logic [47:0] phase_step_offset_q;
  logic [47:0] osc_phase_preload_q;
  logic        commit_trig;
  logic        commit_offset;
  logic        commit_preload;

  logic [31:0] awaddr_latched;
  logic [31:0] wdata_latched;
  logic [ 3:0] wstrb_latched;
  logic        aw_seen;
  logic        w_seen;

  assign s_axi_awready = !aw_seen && !s_axi_bvalid;
  assign s_axi_wready  = !w_seen  && !s_axi_bvalid;
  assign s_axi_arready = !s_axi_rvalid;

  // BRESP/RRESP always OKAY
  assign s_axi_bresp = 2'b00;
  assign s_axi_rresp = 2'b00;

  assign pulse_enable      = reg_control[0];
  assign force_high        = reg_control[2];
  assign harmonic_mode     = reg_control[3];
  assign osc_mode          = reg_control[4];
  assign edge_lock         = reg_control[5];
  assign trig_phase_step   = trig_phase_step_q;
  assign width_n           = reg_width_n;
  assign meas_time_us      = reg_meas_time_us;
  assign phase_step_offset = $signed(phase_step_offset_q);
  assign dwell_cycles      = reg_dwell_cycles;
  assign osc_phase_preload = osc_phase_preload_q;
  assign n_steps           = reg_n_steps;

  always_ff @(posedge clk) begin
    if (!rstn) begin
      reg_control               <= 32'h00000001;
      reg_trig_phase_step_lo    <= 32'h00000000;  // trig_phase_step=0 -> DIO2 off at boot
      reg_width_n               <= 32'h00000001;
      reg_trig_phase_step_hi    <= 16'h0000;
      reg_meas_time_us          <= 32'd100_000;  // Default: 100 ms
      reg_phase_step_offset_lo  <= 32'h00000000;
      reg_phase_step_offset_hi  <= 16'h0000;
      reg_dwell_cycles          <= 32'd0;
      reg_osc_phase_preload_lo  <= 32'h00000000;
      reg_osc_phase_preload_hi  <= 16'h0000;
      reg_n_steps               <= 32'd1;

      trig_phase_step_q         <= 48'd0;
      phase_step_offset_q       <= 48'd0;
      osc_phase_preload_q       <= 48'd0;
      commit_trig               <= 1'b0;
      commit_offset             <= 1'b0;
      commit_preload            <= 1'b0;

      pulse_soft_reset <= 1'b0;

      awaddr_latched   <= 32'd0;
      wdata_latched    <= 32'd0;
      wstrb_latched    <= 4'd0;
      aw_seen          <= 1'b0;
      w_seen           <= 1'b0;

      s_axi_bvalid     <= 1'b0;
      s_axi_rvalid     <= 1'b0;
      s_axi_rdata      <= 32'd0;
    end else begin
      pulse_soft_reset <= 1'b0;

      // ---- 48-bit commit (one cycle after the LO word write) ----
      if (commit_trig)
        trig_phase_step_q   <= {reg_trig_phase_step_hi,   reg_trig_phase_step_lo};
      if (commit_offset)
        phase_step_offset_q <= {reg_phase_step_offset_hi, reg_phase_step_offset_lo};
      if (commit_preload)
        osc_phase_preload_q <= {reg_osc_phase_preload_hi, reg_osc_phase_preload_lo};
      commit_trig    <= 1'b0;
      commit_offset  <= 1'b0;
      commit_preload <= 1'b0;

      // ---- AW channel ----
      if (s_axi_awready && s_axi_awvalid) begin
        awaddr_latched <= s_axi_awaddr;
        aw_seen        <= 1'b1;
      end

      // ---- W channel ----
      if (s_axi_wready && s_axi_wvalid) begin
        wdata_latched <= s_axi_wdata;
        wstrb_latched <= s_axi_wstrb;
        w_seen        <= 1'b1;
      end

      // ---- Write commit ----
      if (aw_seen && w_seen && !s_axi_bvalid) begin
        case (awaddr_latched[6:2])
          5'd0: begin
            if (wstrb_latched[0]) begin
              reg_control[0] <= wdata_latched[0];   // enable
              reg_control[2] <= wdata_latched[2];   // force_high
              reg_control[3] <= wdata_latched[3];   // harmonic_mode
              reg_control[4] <= wdata_latched[4];   // osc_mode
              reg_control[5] <= wdata_latched[5];   // edge_lock
              if (wdata_latched[1])
                pulse_soft_reset <= 1'b1;           // soft_reset strobe
            end
          end

          5'd1: begin
            if (wstrb_latched[0]) reg_trig_phase_step_lo[ 7: 0] <= wdata_latched[ 7: 0];
            if (wstrb_latched[1]) reg_trig_phase_step_lo[15: 8] <= wdata_latched[15: 8];
            if (wstrb_latched[2]) reg_trig_phase_step_lo[23:16] <= wdata_latched[23:16];
            if (wstrb_latched[3]) reg_trig_phase_step_lo[31:24] <= wdata_latched[31:24];
            commit_trig <= 1'b1;
          end

          5'd2: begin  // 0x08  width_n
            if (wstrb_latched[0]) reg_width_n[ 7: 0] <= wdata_latched[ 7: 0];
            if (wstrb_latched[1]) reg_width_n[15: 8] <= wdata_latched[15: 8];
            if (wstrb_latched[2]) reg_width_n[23:16] <= wdata_latched[23:16];
            if (wstrb_latched[3]) reg_width_n[31:24] <= wdata_latched[31:24];
          end

          5'd3: begin  // 0x0C trig_phase_step_hi (bits [47:32] in [15:0])
            if (wstrb_latched[0]) reg_trig_phase_step_hi[ 7:0] <= wdata_latched[ 7:0];
            if (wstrb_latched[1]) reg_trig_phase_step_hi[15:8] <= wdata_latched[15:8];
          end

          // 5'd4 (status), 5'd5 (meas_span), 5'd6 (edge_cnt_out): read-only

          5'd7: begin  // 0x1C phase_step_offset_lo
            if (wstrb_latched[0]) reg_phase_step_offset_lo[ 7: 0] <= wdata_latched[ 7: 0];
            if (wstrb_latched[1]) reg_phase_step_offset_lo[15: 8] <= wdata_latched[15: 8];
            if (wstrb_latched[2]) reg_phase_step_offset_lo[23:16] <= wdata_latched[23:16];
            if (wstrb_latched[3]) reg_phase_step_offset_lo[31:24] <= wdata_latched[31:24];
            commit_offset <= 1'b1;
          end

          5'd8: begin  // 0x20 phase_step_offset_hi (bits [47:32] in [15:0])
            if (wstrb_latched[0]) reg_phase_step_offset_hi[ 7:0] <= wdata_latched[ 7:0];
            if (wstrb_latched[1]) reg_phase_step_offset_hi[15:8] <= wdata_latched[15:8];
          end

          // 5'd9..5'd12 (phase_step_base, phase_step): read-only, writes silently ignored

          5'd13: begin  // 0x34 meas_time_us
            if (wstrb_latched[0]) reg_meas_time_us[ 7: 0] <= wdata_latched[ 7: 0];
            if (wstrb_latched[1]) reg_meas_time_us[15: 8] <= wdata_latched[15: 8];
            if (wstrb_latched[2]) reg_meas_time_us[23:16] <= wdata_latched[23:16];
            if (wstrb_latched[3]) reg_meas_time_us[31:24] <= wdata_latched[31:24];
          end

          5'd14: begin  // 0x38 dwell_cycles
            if (wstrb_latched[0]) reg_dwell_cycles[ 7: 0] <= wdata_latched[ 7: 0];
            if (wstrb_latched[1]) reg_dwell_cycles[15: 8] <= wdata_latched[15: 8];
            if (wstrb_latched[2]) reg_dwell_cycles[23:16] <= wdata_latched[23:16];
            if (wstrb_latched[3]) reg_dwell_cycles[31:24] <= wdata_latched[31:24];
          end

          5'd15: begin  // 0x3C osc_phase_preload_lo
            if (wstrb_latched[0]) reg_osc_phase_preload_lo[ 7: 0] <= wdata_latched[ 7: 0];
            if (wstrb_latched[1]) reg_osc_phase_preload_lo[15: 8] <= wdata_latched[15: 8];
            if (wstrb_latched[2]) reg_osc_phase_preload_lo[23:16] <= wdata_latched[23:16];
            if (wstrb_latched[3]) reg_osc_phase_preload_lo[31:24] <= wdata_latched[31:24];
            commit_preload <= 1'b1;
          end

          5'd16: begin  // 0x40 osc_phase_preload_hi (bits [47:32] in [15:0])
            if (wstrb_latched[0]) reg_osc_phase_preload_hi[ 7:0] <= wdata_latched[ 7:0];
            if (wstrb_latched[1]) reg_osc_phase_preload_hi[15:8] <= wdata_latched[15:8];
          end

          5'd17: begin  // 0x44 n_steps
            if (wstrb_latched[0]) reg_n_steps[ 7: 0] <= wdata_latched[ 7: 0];
            if (wstrb_latched[1]) reg_n_steps[15: 8] <= wdata_latched[15: 8];
            if (wstrb_latched[2]) reg_n_steps[23:16] <= wdata_latched[23:16];
            if (wstrb_latched[3]) reg_n_steps[31:24] <= wdata_latched[31:24];
          end

          // 5'd18 (step_index): read-only

          default: begin end
        endcase

        aw_seen      <= 1'b0;
        w_seen       <= 1'b0;
        s_axi_bvalid <= 1'b1;
      end else if (s_axi_bvalid && s_axi_bready) begin
        s_axi_bvalid <= 1'b0;
      end

      // ---- Read channel ----
      if (s_axi_arready && s_axi_arvalid) begin
        case (s_axi_araddr[6:2])
          // bit 1 (soft_reset) always reads 0; all other control bits read back as stored
          5'd0:  s_axi_rdata <= {reg_control[31:2], 1'b0, reg_control[0]};
          5'd1:  s_axi_rdata <= reg_trig_phase_step_lo;
          5'd2:  s_axi_rdata <= reg_width_n;
          5'd3:  s_axi_rdata <= {16'd0, reg_trig_phase_step_hi};
          5'd4:  s_axi_rdata <= {26'd0, strobe_done, freerun_active, timeout_flag,
                                         period_stable, period_valid, pulse_busy};
          5'd5:  s_axi_rdata <= meas_span;
          5'd6:  s_axi_rdata <= edge_cnt_out;
          5'd7:  s_axi_rdata <= reg_phase_step_offset_lo;
          5'd8:  s_axi_rdata <= {16'd0, reg_phase_step_offset_hi};
          5'd9:  s_axi_rdata <= phase_step_base[31:0];
          5'd10: s_axi_rdata <= {16'd0, phase_step_base[47:32]};
          5'd11: s_axi_rdata <= phase_step[31:0];
          5'd12: s_axi_rdata <= {16'd0, phase_step[47:32]};
          5'd13: s_axi_rdata <= reg_meas_time_us;
          5'd14: s_axi_rdata <= reg_dwell_cycles;
          5'd15: s_axi_rdata <= reg_osc_phase_preload_lo;
          5'd16: s_axi_rdata <= {16'd0, reg_osc_phase_preload_hi};
          5'd17: s_axi_rdata <= reg_n_steps;
          5'd18: s_axi_rdata <= step_index;
          default: s_axi_rdata <= 32'h00000000;
        endcase
        s_axi_rvalid <= 1'b1;
      end else if (s_axi_rvalid && s_axi_rready) begin
        s_axi_rvalid <= 1'b0;
      end
    end
  end

endmodule

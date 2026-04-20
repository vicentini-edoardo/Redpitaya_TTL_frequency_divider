`timescale 1ns / 1ps
// axi4lite_pulse_regs - AXI4-Lite slave register file for pulse_gen.
//
// Clock domain: fclk_clk0 (PS GP0 master clock).
//               All register outputs drive pulse_gen on the same clock - no CDC needed.
//
// Register map (byte offset / 32-bit word):
//   0x00 RW  control:  [0] pulse_enable, [1] soft_reset (self-clearing, reads 0)
//   0x04 RW  pulse_divider  (kept for address stability; unused by pulse_gen)
//   0x08 RW  pulse_width    (clock cycles, 8 ns/step)
//   0x0C RW  pulse_delay    (kept for address stability; unused by pulse_gen)
//   0x10 RO  status:   [0] busy, [1] period_valid, [2] timeout, [3] period_stable,
//                      [4] freerun_active
//   0x14 RO  period_cycles       (instantaneous trigger period)
//   0x18 RO  period_avg_cycles   (IIR-filtered period, alpha = 1/8)
//   0x1C RW  phase_step_offset_lo   bits [31:0]  of signed 48-bit NCO offset
//   0x20 RW  phase_step_offset_hi   bits [47:32] of signed 48-bit NCO offset (in [15:0])
//   0x24 RO  phase_step_base_lo     bits [31:0]  of computed base step
//   0x28 RO  phase_step_base_hi     bits [47:32] of computed base step (in [15:0])
//   0x2C RO  phase_step_lo          bits [31:0]  of live phase_step
//   0x30 RO  phase_step_hi          bits [47:32] of live phase_step (in [15:0])
//
// AXI4-Lite write handshake:
//   aw_seen / w_seen track independent acceptance of AW and W channels.
//   Both must arrive before the write is committed and BVALID is asserted.
//
// Notes:
//   soft_reset (control[1]) is self-clearing - it asserts pulse_soft_reset
//   for exactly one clock cycle and always reads back as 0.
//   Writes to read-only or undefined addresses are silently accepted
//   (BRESP = OKAY) to avoid AXI bus hangs on simple PS drivers.

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
  output logic [31:0] pulse_divider,
  output logic [31:0] pulse_width,
  output logic [31:0] pulse_delay,
  output logic signed [47:0] phase_step_offset,

  // Status inputs <- pulse_gen
  input  logic        pulse_busy,
  input  logic [31:0] period_cycles,
  input  logic [31:0] period_avg_cycles,
  input  logic        period_valid,
  input  logic        period_stable,
  input  logic        timeout_flag,
  input  logic        freerun_active,
  input  logic signed [47:0] phase_step_base,
  input  logic signed [47:0] phase_step
);

  logic [31:0] reg_control;
  logic [31:0] reg_divider;
  logic [31:0] reg_width;
  logic [31:0] reg_delay;
  logic [31:0] reg_phase_step_offset_lo;
  logic [15:0] reg_phase_step_offset_hi;

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
  assign pulse_divider     = reg_divider;
  assign pulse_width       = reg_width;
  assign pulse_delay       = reg_delay;
  assign phase_step_offset = $signed({reg_phase_step_offset_hi, reg_phase_step_offset_lo});

  always_ff @(posedge clk) begin
    if (!rstn) begin
      reg_control               <= 32'h00000001;
      reg_divider               <= 32'h00000001;
      reg_width                 <= 32'h00000001;
      reg_delay                 <= 32'h00000001;
      reg_phase_step_offset_lo  <= 32'h00000000;
      reg_phase_step_offset_hi  <= 16'h0000;

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
        case (awaddr_latched[5:2])
          4'd0: begin
            if (wstrb_latched[0]) begin
              reg_control[0] <= wdata_latched[0];
              if (wdata_latched[1])
                pulse_soft_reset <= 1'b1;
            end
          end

          4'd1: begin
            if (wstrb_latched[0]) reg_divider[ 7: 0] <= wdata_latched[ 7: 0];
            if (wstrb_latched[1]) reg_divider[15: 8] <= wdata_latched[15: 8];
            if (wstrb_latched[2]) reg_divider[23:16] <= wdata_latched[23:16];
            if (wstrb_latched[3]) reg_divider[31:24] <= wdata_latched[31:24];
          end

          4'd2: begin
            if (wstrb_latched[0]) reg_width[ 7: 0] <= wdata_latched[ 7: 0];
            if (wstrb_latched[1]) reg_width[15: 8] <= wdata_latched[15: 8];
            if (wstrb_latched[2]) reg_width[23:16] <= wdata_latched[23:16];
            if (wstrb_latched[3]) reg_width[31:24] <= wdata_latched[31:24];
          end

          4'd3: begin
            if (wstrb_latched[0]) reg_delay[ 7: 0] <= wdata_latched[ 7: 0];
            if (wstrb_latched[1]) reg_delay[15: 8] <= wdata_latched[15: 8];
            if (wstrb_latched[2]) reg_delay[23:16] <= wdata_latched[23:16];
            if (wstrb_latched[3]) reg_delay[31:24] <= wdata_latched[31:24];
          end

          // 4'd4 (status), 4'd5 (period_cycles), 4'd6 (period_avg): read-only

          4'd7: begin  // 0x1C phase_step_offset_lo
            if (wstrb_latched[0]) reg_phase_step_offset_lo[ 7: 0] <= wdata_latched[ 7: 0];
            if (wstrb_latched[1]) reg_phase_step_offset_lo[15: 8] <= wdata_latched[15: 8];
            if (wstrb_latched[2]) reg_phase_step_offset_lo[23:16] <= wdata_latched[23:16];
            if (wstrb_latched[3]) reg_phase_step_offset_lo[31:24] <= wdata_latched[31:24];
          end

          4'd8: begin  // 0x20 phase_step_offset_hi (bits [47:32] of offset, in [15:0])
            if (wstrb_latched[0]) reg_phase_step_offset_hi[ 7:0] <= wdata_latched[ 7:0];
            if (wstrb_latched[1]) reg_phase_step_offset_hi[15:8] <= wdata_latched[15:8];
            // strobe[2] and strobe[3] map above bit 15 - no register bits there
          end

          // 4'd9..4'd12 (phase_step_base, phase_step): read-only, writes silently ignored

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
        case (s_axi_araddr[5:2])
          4'd0:  s_axi_rdata <= {reg_control[31:2], 1'b0, reg_control[0]};
          4'd1:  s_axi_rdata <= reg_divider;
          4'd2:  s_axi_rdata <= reg_width;
          4'd3:  s_axi_rdata <= reg_delay;
          4'd4:  s_axi_rdata <= {27'd0, freerun_active, timeout_flag,
                                         period_stable, period_valid, pulse_busy};
          4'd5:  s_axi_rdata <= period_cycles;
          4'd6:  s_axi_rdata <= period_avg_cycles;
          4'd7:  s_axi_rdata <= reg_phase_step_offset_lo;
          4'd8:  s_axi_rdata <= {16'd0, reg_phase_step_offset_hi};
          4'd9:  s_axi_rdata <= phase_step_base[31:0];
          4'd10: s_axi_rdata <= {16'd0, phase_step_base[47:32]};
          4'd11: s_axi_rdata <= phase_step[31:0];
          4'd12: s_axi_rdata <= {16'd0, phase_step[47:32]};
          default: s_axi_rdata <= 32'h00000000;
        endcase
        s_axi_rvalid <= 1'b1;
      end else if (s_axi_rvalid && s_axi_rready) begin
        s_axi_rvalid <= 1'b0;
      end
    end
  end

endmodule

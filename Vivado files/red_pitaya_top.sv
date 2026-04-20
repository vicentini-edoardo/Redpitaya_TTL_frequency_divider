`timescale 1ns / 1ps

////////////////////////////////////////////////////////////////////////////////
// Red Pitaya TOP module. It connects external pins and PS part with
// other application modules.
// Authors: Matej Oblak, Iztok Jeras
// (c) Red Pitaya  http://www.redpitaya.com
////////////////////////////////////////////////////////////////////////////////

module red_pitaya_top
(
  inout  logic [7:0] exp_p_io,
  inout  logic [7:0] exp_n_io,

  // PS connections
  inout  logic [53:0] FIXED_IO_mio,
  inout  logic        FIXED_IO_ps_clk,
  inout  logic        FIXED_IO_ps_porb,
  inout  logic        FIXED_IO_ps_srstb,
  inout  logic        FIXED_IO_ddr_vrn,
  inout  logic        FIXED_IO_ddr_vrp,

  // DDR
  inout  logic [14:0] DDR_addr,
  inout  logic [ 2:0] DDR_ba,
  inout  logic        DDR_cas_n,
  inout  logic        DDR_ck_n,
  inout  logic        DDR_ck_p,
  inout  logic        DDR_cke,
  inout  logic        DDR_cs_n,
  inout  logic [ 3:0] DDR_dm,
  inout  logic [31:0] DDR_dq,
  inout  logic [ 3:0] DDR_dqs_n,
  inout  logic [ 3:0] DDR_dqs_p,
  inout  logic        DDR_odt,
  inout  logic        DDR_ras_n,
  inout  logic        DDR_reset_n,
  inout  logic        DDR_we_n
);

logic fclk_clk0;
logic fclk_reset0_n;

// AXI GP0 from PS
logic [31:0] m_axi_gp0_araddr;
logic [ 1:0] m_axi_gp0_arburst;
logic [ 3:0] m_axi_gp0_arcache;
logic [11:0] m_axi_gp0_arid;
logic [ 3:0] m_axi_gp0_arlen;
logic [ 1:0] m_axi_gp0_arlock;
logic [ 2:0] m_axi_gp0_arprot;
logic [ 3:0] m_axi_gp0_arqos;
logic        m_axi_gp0_arready;
logic [ 2:0] m_axi_gp0_arsize;
logic        m_axi_gp0_arvalid;

logic [31:0] m_axi_gp0_awaddr;
logic [ 1:0] m_axi_gp0_awburst;
logic [ 3:0] m_axi_gp0_awcache;
logic [11:0] m_axi_gp0_awid;
logic [ 3:0] m_axi_gp0_awlen;
logic [ 1:0] m_axi_gp0_awlock;
logic [ 2:0] m_axi_gp0_awprot;
logic [ 3:0] m_axi_gp0_awqos;
logic        m_axi_gp0_awready;
logic [ 2:0] m_axi_gp0_awsize;
logic        m_axi_gp0_awvalid;

logic [11:0] m_axi_gp0_bid;
logic        m_axi_gp0_bready;
logic [ 1:0] m_axi_gp0_bresp;
logic        m_axi_gp0_bvalid;

logic [31:0] m_axi_gp0_rdata;
logic [11:0] m_axi_gp0_rid;
logic        m_axi_gp0_rlast;
logic        m_axi_gp0_rready;
logic [ 1:0] m_axi_gp0_rresp;
logic        m_axi_gp0_rvalid;

logic [31:0] m_axi_gp0_wdata;
logic [11:0] m_axi_gp0_wid;
logic        m_axi_gp0_wlast;
logic        m_axi_gp0_wready;
logic [ 3:0] m_axi_gp0_wstrb;
logic        m_axi_gp0_wvalid;

// Pulse control and status
logic        pulse_enable;
logic        pulse_soft_reset;
logic [31:0] pulse_divider;
logic [31:0] pulse_width;
logic [31:0] pulse_delay;
logic        pulse_busy;
logic        pulse_out;

logic [31:0] period_cycles;
logic [31:0] period_avg_cycles;
logic        period_valid;
logic        period_stable;
logic        timeout_flag;

// NCO phase step registers
logic signed [47:0] phase_step_offset;
logic signed [47:0] phase_step_base;
logic signed [47:0] phase_step;
logic        freerun_active;

logic        trig_rise_dbg;

// Drive the pulse output on exp_p_io[1]
// exp_p_io[0] is the trigger input (no driver needed - used as input)
// exp_p_io[2..7] are unused, left tri-stated
assign exp_p_io[1] = pulse_out;

system_wrapper system_i
(
  .FIXED_IO_mio      (FIXED_IO_mio),
  .FIXED_IO_ps_clk   (FIXED_IO_ps_clk),
  .FIXED_IO_ps_porb  (FIXED_IO_ps_porb),
  .FIXED_IO_ps_srstb (FIXED_IO_ps_srstb),
  .FIXED_IO_ddr_vrn  (FIXED_IO_ddr_vrn),
  .FIXED_IO_ddr_vrp  (FIXED_IO_ddr_vrp),

  .DDR_addr          (DDR_addr),
  .DDR_ba            (DDR_ba),
  .DDR_cas_n         (DDR_cas_n),
  .DDR_ck_n          (DDR_ck_n),
  .DDR_ck_p          (DDR_ck_p),
  .DDR_cke           (DDR_cke),
  .DDR_cs_n          (DDR_cs_n),
  .DDR_dm            (DDR_dm),
  .DDR_dq            (DDR_dq),
  .DDR_dqs_n         (DDR_dqs_n),
  .DDR_dqs_p         (DDR_dqs_p),
  .DDR_odt           (DDR_odt),
  .DDR_ras_n         (DDR_ras_n),
  .DDR_reset_n       (DDR_reset_n),
  .DDR_we_n          (DDR_we_n),

  .FCLK_CLK0_0       (fclk_clk0),
  .FCLK_RESET0_N_0   (fclk_reset0_n),

  .M_AXI_GP0_0_araddr  (m_axi_gp0_araddr),
  .M_AXI_GP0_0_arburst (m_axi_gp0_arburst),
  .M_AXI_GP0_0_arcache (m_axi_gp0_arcache),
  .M_AXI_GP0_0_arid    (m_axi_gp0_arid),
  .M_AXI_GP0_0_arlen   (m_axi_gp0_arlen),
  .M_AXI_GP0_0_arlock  (m_axi_gp0_arlock),
  .M_AXI_GP0_0_arprot  (m_axi_gp0_arprot),
  .M_AXI_GP0_0_arqos   (m_axi_gp0_arqos),
  .M_AXI_GP0_0_arready (m_axi_gp0_arready),
  .M_AXI_GP0_0_arsize  (m_axi_gp0_arsize),
  .M_AXI_GP0_0_arvalid (m_axi_gp0_arvalid),

  .M_AXI_GP0_0_awaddr  (m_axi_gp0_awaddr),
  .M_AXI_GP0_0_awburst (m_axi_gp0_awburst),
  .M_AXI_GP0_0_awcache (m_axi_gp0_awcache),
  .M_AXI_GP0_0_awid    (m_axi_gp0_awid),
  .M_AXI_GP0_0_awlen   (m_axi_gp0_awlen),
  .M_AXI_GP0_0_awlock  (m_axi_gp0_awlock),
  .M_AXI_GP0_0_awprot  (m_axi_gp0_awprot),
  .M_AXI_GP0_0_awqos   (m_axi_gp0_awqos),
  .M_AXI_GP0_0_awready (m_axi_gp0_awready),
  .M_AXI_GP0_0_awsize  (m_axi_gp0_awsize),
  .M_AXI_GP0_0_awvalid (m_axi_gp0_awvalid),

  .M_AXI_GP0_0_bid     (m_axi_gp0_bid),
  .M_AXI_GP0_0_bready  (m_axi_gp0_bready),
  .M_AXI_GP0_0_bresp   (m_axi_gp0_bresp),
  .M_AXI_GP0_0_bvalid  (m_axi_gp0_bvalid),

  .M_AXI_GP0_0_rdata   (m_axi_gp0_rdata),
  .M_AXI_GP0_0_rid     (m_axi_gp0_rid),
  .M_AXI_GP0_0_rlast   (m_axi_gp0_rlast),
  .M_AXI_GP0_0_rready  (m_axi_gp0_rready),
  .M_AXI_GP0_0_rresp   (m_axi_gp0_rresp),
  .M_AXI_GP0_0_rvalid  (m_axi_gp0_rvalid),

  .M_AXI_GP0_0_wdata   (m_axi_gp0_wdata),
  .M_AXI_GP0_0_wid     (m_axi_gp0_wid),
  .M_AXI_GP0_0_wlast   (m_axi_gp0_wlast),
  .M_AXI_GP0_0_wready  (m_axi_gp0_wready),
  .M_AXI_GP0_0_wstrb   (m_axi_gp0_wstrb),
  .M_AXI_GP0_0_wvalid  (m_axi_gp0_wvalid)
);

pulse_gen pulse_gen_i
(
  .clk               (fclk_clk0),
  .rstn              (fclk_reset0_n),

  .enable            (pulse_enable),
  .soft_reset        (pulse_soft_reset),

  .trig_in           (exp_p_io[0]),

  .pulse_width        (pulse_width),
  .phase_step_offset  (phase_step_offset),

  .trig_rise_dbg      (trig_rise_dbg),

  .pulse_out          (pulse_out),
  .busy               (pulse_busy),

  .period_cycles      (period_cycles),
  .period_avg_cycles  (period_avg_cycles),
  .period_valid       (period_valid),
  .period_stable      (period_stable),
  .timeout_flag       (timeout_flag),

  .freerun_active     (freerun_active),
  .phase_step_base    (phase_step_base),
  .phase_step         (phase_step)
);

axi4lite_pulse_regs regs_i
(
  .clk                 (fclk_clk0),
  .rstn                (fclk_reset0_n),

  .s_axi_awaddr        (m_axi_gp0_awaddr),
  .s_axi_awvalid       (m_axi_gp0_awvalid),
  .s_axi_awready       (m_axi_gp0_awready),

  .s_axi_wdata         (m_axi_gp0_wdata),
  .s_axi_wstrb         (m_axi_gp0_wstrb),
  .s_axi_wvalid        (m_axi_gp0_wvalid),
  .s_axi_wready        (m_axi_gp0_wready),

  .s_axi_bresp         (m_axi_gp0_bresp),
  .s_axi_bvalid        (m_axi_gp0_bvalid),
  .s_axi_bready        (m_axi_gp0_bready),

  .s_axi_araddr        (m_axi_gp0_araddr),
  .s_axi_arvalid       (m_axi_gp0_arvalid),
  .s_axi_arready       (m_axi_gp0_arready),

  .s_axi_rdata         (m_axi_gp0_rdata),
  .s_axi_rresp         (m_axi_gp0_rresp),
  .s_axi_rvalid        (m_axi_gp0_rvalid),
  .s_axi_rready        (m_axi_gp0_rready),

  .pulse_enable        (pulse_enable),
  .pulse_soft_reset    (pulse_soft_reset),
  .pulse_divider       (pulse_divider),
  .pulse_width         (pulse_width),
  .pulse_delay         (pulse_delay),
  .phase_step_offset   (phase_step_offset),

  .pulse_busy          (pulse_busy),
  .period_cycles       (period_cycles),
  .period_avg_cycles   (period_avg_cycles),
  .period_valid        (period_valid),
  .period_stable       (period_stable),
  .timeout_flag        (timeout_flag),
  .freerun_active      (freerun_active),
  .phase_step_base     (phase_step_base),
  .phase_step          (phase_step)
);

assign m_axi_gp0_bid   = m_axi_gp0_awid;
assign m_axi_gp0_rid   = m_axi_gp0_arid;
assign m_axi_gp0_rlast = 1'b1;

endmodule

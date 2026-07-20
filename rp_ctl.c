/*
 * rp_ctl.c — Red Pitaya unified NCO helper (pulse mode + harmonic mode).
 *
 * Single binary compiled from this source and symlinked to two names:
 *   /root/rp_pulse_ctl    → pulse mode   (harmonic_mode bit = 0)
 *   /root/rp_harmonic_ctl → harmonic mode (harmonic_mode bit = 1)
 *
 * Mode is selected at runtime by the binary name (argv[0]):
 *   basename contains "harmonic"  → harmonic mode
 *   otherwise                     → pulse mode
 *
 * Compile and install on the board:
 *   gcc -O2 -o /root/rp_ctl rp_ctl.c
 *   ln -sf /root/rp_ctl /root/rp_pulse_ctl
 *   ln -sf /root/rp_ctl /root/rp_harmonic_ctl
 *
 * Register map (base address passed as first argument, default 0x40600000):
 *   0x00  control           bit 0 = enable, bit 1 = soft_reset (self-clearing),
 *                           bit 2 = force_high, bit 3 = harmonic_mode,
 *                           bit 4 = osc_mode, bit 5 = edge_lock
 *   0x04  trig_phase_step_lo  bits [31:0]  of DIO2 48-bit NCO step (0=off)
 *   0x08  reg08             pulse: width_n (clock cycles)
 *                           harmonic: mult_n (bits [2:0], clamped to [1..5])
 *   0x0C  trig_phase_step_hi  bits [47:32] of DIO2 48-bit NCO step
 *   0x10  status            bit 0 = busy, bit 1 = period_valid, bit 2 = period_stable,
 *                           bit 3 = timeout, bit 4 = freerun_active, bit 5 = strobe_done
 *   0x14  meas_span         clock cycles between first and last rising edge of last window
 *   0x18  edge_cnt          rising-edge count from last measurement window
 *   0x1C  phase_step_offset_lo  bits [31:0]  of signed 48-bit NCO offset
 *   0x20  phase_step_offset_hi  bits [47:32] of signed 48-bit NCO offset (in [15:0])
 *   0x24  phase_step_base_lo    bits [31:0]  of computed base step (read-only)
 *   0x28  phase_step_base_hi    bits [47:32] of computed base step (in [15:0])
 *   0x2C  phase_step_lo         bits [31:0]  of live phase_step (read-only)
 *   0x30  phase_step_hi         bits [47:32] of live phase_step (in [15:0])
 *   0x34  meas_time_us      measurement window in microseconds (min 1000)
 *   0x38  dwell_cycles      clock ticks per strobe point (osc mode)
 *   0x3C  osc_phase_preload_lo  bits [31:0]  of 48-bit start-phase preload
 *   0x40  osc_phase_preload_hi  bits [47:32] of 48-bit start-phase preload
 *   0x44  n_steps           strobe points per scan (osc mode, >=1)
 *   0x48  step_index        current strobe point, 0-based (read-only)
 *
 * In osc mode phase_step_offset (0x1C/0x20) is the per-step target
 * increment: two's-complement of round(step_frac * 2^48).
 *
 * JSON output fields (same for both modes):
 *   control, harmonic_mode, osc_mode, edge_lock, force_high, width, mult_n,
 *   trig_phase_step, status, period_stable, freerun_active, strobe_done,
 *   meas_time_us, meas_span, edge_cnt,
 *   phase_step_offset, phase_step_base, phase_step,
 *   dwell_cycles, osc_phase_preload, n_steps, step_index
 *
 * Frequency conversions (use phase_step_base for accuracy):
 *   input_hz  = 124999999.0 * phase_step_base / 2^48
 *             = 124999999.0 * (edge_cnt - 1) / meas_span
 *   output_hz = phase_step * 124999999.0 / 2^48
 *   NCO res   ≈ 0.44 mHz per LSB at 124.999999 MHz
 */

#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <fcntl.h>
#include <sys/mman.h>
#include <unistd.h>

#define REG_CONTROL               0x00
#define REG_TRIG_PHASE_STEP_LO    0x04
#define REG_REG08                 0x08
#define REG_TRIG_PHASE_STEP_HI    0x0C
#define REG_STATUS                0x10
#define REG_MEAS_SPAN             0x14
#define REG_EDGE_CNT              0x18
#define REG_PHASE_STEP_OFFSET_LO  0x1C
#define REG_PHASE_STEP_OFFSET_HI  0x20
#define REG_PHASE_STEP_BASE_LO    0x24
#define REG_PHASE_STEP_BASE_HI    0x28
#define REG_PHASE_STEP_LO         0x2C
#define REG_PHASE_STEP_HI         0x30
#define REG_MEAS_TIME_US          0x34
#define REG_DWELL_CYCLES          0x38
#define REG_OSC_PHASE_PRELOAD_LO  0x3C
#define REG_OSC_PHASE_PRELOAD_HI  0x40
#define REG_N_STEPS               0x44
#define REG_STEP_INDEX            0x48

#define CTRL_ENABLE       0x01u
#define CTRL_SOFT_RESET   0x02u
#define CTRL_FORCE_HIGH   0x04u
#define CTRL_HARMONIC     0x08u
#define CTRL_OSC_MODE     0x10u   /* bit 4 — stepped strobe scan */
#define CTRL_EDGE_LOCK    0x20u   /* bit 5 — anchor NCO phase to input edges */

/* Bits the caller may pass; harmonic_mode is managed internally */
#define CTRL_USER_MASK    (CTRL_ENABLE | CTRL_FORCE_HIGH | CTRL_OSC_MODE | CTRL_EDGE_LOCK)

static int g_harmonic;   /* 0 = pulse mode, 1 = harmonic mode */

static int detect_mode(const char *progname) {
    const char *base = strrchr(progname, '/');
    base = base ? base + 1 : progname;
    return (strstr(base, "harmonic") != NULL);
}

static void usage(const char *prog) {
    const char *arg3 = g_harmonic ? "mult_n (1..5)" : "width_cycles";
    fprintf(stderr,
        "Usage:\n"
        "  %s <base_addr> read\n"
        "  %s <base_addr> write <%s> <phase_step_offset> <control>\n"
        "      phase_step_offset: signed 48-bit integer\n"
        "      control: bit 0=enable, bit 2=force_high, bit 4=osc_mode, bit 5=edge_lock\n"
        "  %s <base_addr> control <value>\n"
        "      Set only the control register (0=off, 1=modulated, 4=force-high/on).\n"
        "  %s <base_addr> window <microseconds>\n"
        "      e.g. 1000=1ms  10000=10ms  100000=100ms  500000=500ms  1000000=1s\n"
        "  %s <base_addr> trig <phase_step>\n"
        "      DIO2 square wave: phase_step = round(f_hz * 2^48 / 124999999). 0=off.\n"
        "      e.g. 2251800=1Hz  225179983=100Hz  2251799832=1000Hz\n"
        "  %s <base_addr> osc <dwell_cycles> <phase_preload_uint64> <n_steps>\n"
        "      Set stepped-strobe registers (dwell_cycles, start-phase preload, n_steps).\n"
        "      Step size goes in phase_step_offset via the write command; re-arm the\n"
        "      scan by toggling bit4 off then on (write with bit4=1 in control).\n"
        "  %s <base_addr> preload <phase_preload_uint64>\n"
        "      Set osc_phase_preload only (edge-lock phase offset); leaves dwell_cycles.\n"
        "      Re-arm edge_lock (bit5 off→on via write) to apply the new phase.\n"
        "  %s <base_addr> soft_reset\n",
        prog, prog, arg3, prog, prog, prog, prog, prog, prog);
}

static uint32_t rd32(volatile uint8_t *base, off_t off) {
    return *(volatile uint32_t *)(base + off);
}

static void wr32(volatile uint8_t *base, off_t off, uint32_t val) {
    *(volatile uint32_t *)(base + off) = val;
}

static int64_t rd48(volatile uint8_t *base, off_t lo_off, off_t hi_off) {
    uint64_t lo  = rd32(base, lo_off);
    uint64_t hi  = rd32(base, hi_off) & 0xFFFFu;
    uint64_t raw = (hi << 32) | lo;
    if (raw & (UINT64_C(1) << 47))
        raw |= ~((UINT64_C(1) << 48) - 1);
    return (int64_t)raw;
}

static uint64_t rd48u(volatile uint8_t *base, off_t lo_off, off_t hi_off) {
    uint64_t lo = rd32(base, lo_off);
    uint64_t hi = rd32(base, hi_off) & 0xFFFFu;
    return (hi << 32) | lo;
}

/* Write high word first so the FPGA latches the full 48-bit value atomically. */
static void wr48(volatile uint8_t *base, off_t lo_off, off_t hi_off, int64_t val) {
    uint64_t bits = (uint64_t)val & ((UINT64_C(1) << 48) - 1);
    wr32(base, hi_off, (uint32_t)((bits >> 32) & 0xFFFFu));
    wr32(base, lo_off, (uint32_t)(bits & 0xFFFFFFFFu));
}

static void wr48u(volatile uint8_t *base, off_t lo_off, off_t hi_off, uint64_t val) {
    uint64_t bits = val & ((UINT64_C(1) << 48) - 1);
    wr32(base, hi_off, (uint32_t)((bits >> 32) & 0xFFFFu));
    wr32(base, lo_off, (uint32_t)(bits & 0xFFFFFFFFu));
}

static void print_json(volatile uint8_t *base) {
    const uint32_t control           = rd32(base, REG_CONTROL);
    const uint32_t harmonic_mode     = (control >> 3) & 0x1u;
    const uint32_t osc_mode          = (control >> 4) & 0x1u;
    const uint32_t edge_lock         = (control >> 5) & 0x1u;
    const uint32_t force_high        = (control >> 2) & 0x1u;
    const uint32_t reg08             = rd32(base, REG_REG08);
    const uint32_t mult_n            = (reg08 < 1u) ? 1u : (reg08 > 5u) ? 5u : reg08;
    const uint64_t trig_phase_step   = rd48u(base, REG_TRIG_PHASE_STEP_LO, REG_TRIG_PHASE_STEP_HI);
    const uint32_t status            = rd32(base, REG_STATUS);
    const uint32_t period_stable     = (status >> 2) & 0x1u;
    const uint32_t freerun_active    = (status >> 4) & 0x1u;
    const uint32_t strobe_done       = (status >> 5) & 0x1u;
    const uint32_t meas_time_us      = rd32(base, REG_MEAS_TIME_US);
    const int64_t  step_offset       = rd48(base, REG_PHASE_STEP_OFFSET_LO, REG_PHASE_STEP_OFFSET_HI);
    const int64_t  step_base         = rd48(base, REG_PHASE_STEP_BASE_LO,   REG_PHASE_STEP_BASE_HI);
    const int64_t  step_live         = rd48(base, REG_PHASE_STEP_LO,        REG_PHASE_STEP_HI);
    const uint32_t dwell_cycles      = rd32(base, REG_DWELL_CYCLES);
    const uint64_t osc_phase_preload = rd48u(base, REG_OSC_PHASE_PRELOAD_LO, REG_OSC_PHASE_PRELOAD_HI);
    const uint32_t n_steps           = rd32(base, REG_N_STEPS);
    const uint32_t step_index        = rd32(base, REG_STEP_INDEX);

    printf("{\"control\":%u,\"harmonic_mode\":%u,\"osc_mode\":%u,\"edge_lock\":%u,\"force_high\":%u,"
           "\"width\":%u,\"mult_n\":%u,\"trig_phase_step\":%llu,"
           "\"status\":%u,\"period_stable\":%u,\"freerun_active\":%u,\"strobe_done\":%u,"
           "\"meas_time_us\":%u,\"meas_span\":%u,\"edge_cnt\":%u,"
           "\"phase_step_offset\":%lld,\"phase_step_base\":%lld,\"phase_step\":%lld,"
           "\"dwell_cycles\":%u,\"osc_phase_preload\":%llu,"
           "\"n_steps\":%u,\"step_index\":%u}\n",
           control,
           harmonic_mode,
           osc_mode,
           edge_lock,
           force_high,
           reg08,
           mult_n,
           (unsigned long long)trig_phase_step,
           status,
           period_stable,
           freerun_active,
           strobe_done,
           meas_time_us,
           rd32(base, REG_MEAS_SPAN),
           rd32(base, REG_EDGE_CNT),
           (long long)step_offset,
           (long long)step_base,
           (long long)step_live,
           dwell_cycles,
           (unsigned long long)osc_phase_preload,
           n_steps,
           step_index);
}

int main(int argc, char **argv) {
    int fd;
    void *map;
    volatile uint8_t *base;
    off_t phys;
    long page_size;
    off_t page_base;
    off_t page_off;

    g_harmonic = detect_mode(argv[0]);

    if (argc < 3) {
        usage(argv[0]);
        return 1;
    }

    phys      = (off_t)strtoull(argv[1], NULL, 0);
    page_size = sysconf(_SC_PAGESIZE);
    page_base = phys & ~((off_t)page_size - 1);
    page_off  = phys - page_base;

    fd = open("/dev/mem", O_RDWR | O_SYNC);
    if (fd < 0) { perror("open"); return 1; }

    map = mmap(NULL, (size_t)page_size, PROT_READ | PROT_WRITE, MAP_SHARED, fd, page_base);
    if (map == MAP_FAILED) { perror("mmap"); close(fd); return 1; }

    base = (volatile uint8_t *)map + page_off;

    if (strcmp(argv[2], "read") == 0) {
        print_json(base);

    } else if (strcmp(argv[2], "write") == 0) {
        if (argc != 6) {
            usage(argv[0]);
            munmap(map, (size_t)page_size);
            close(fd);
            return 1;
        }
        uint32_t reg08_val         = (uint32_t)strtoul(argv[3], NULL, 0);
        int64_t  phase_step_offset = (int64_t)strtoll(argv[4], NULL, 0);
        uint32_t user_ctrl         = (uint32_t)strtoul(argv[5], NULL, 0) & CTRL_USER_MASK;

        if (g_harmonic) {
            /* Clamp mult_n to [1, 5]; osc_mode is a pulse-mode feature */
            if (reg08_val < 1u) reg08_val = 1u;
            if (reg08_val > 5u) reg08_val = 5u;
            user_ctrl &= ~CTRL_OSC_MODE;
            user_ctrl |= CTRL_HARMONIC;
        }
        /* Registers first, control last. The FPGA commits 48-bit values
         * atomically on their low-word write, so the output keeps running
         * during an update — no disable/re-enable cycle, which would reset
         * the frequency measurement and drop the output for a full window. */
        wr32(base, REG_REG08, reg08_val);
        wr48(base, REG_PHASE_STEP_OFFSET_LO, REG_PHASE_STEP_OFFSET_HI, phase_step_offset);
        wr32(base, REG_CONTROL, user_ctrl);
        print_json(base);

    } else if (strcmp(argv[2], "control") == 0) {
        if (argc != 4) {
            usage(argv[0]);
            munmap(map, (size_t)page_size);
            close(fd);
            return 1;
        }
        uint32_t ctrl = (uint32_t)strtoul(argv[3], NULL, 0) & CTRL_USER_MASK;
        if (g_harmonic) {
            ctrl &= ~CTRL_OSC_MODE;   /* osc_mode is a pulse-mode feature */
            ctrl |= CTRL_HARMONIC;
        }
        wr32(base, REG_CONTROL, ctrl);
        print_json(base);

    } else if (strcmp(argv[2], "soft_reset") == 0) {
        uint32_t ctrl = rd32(base, REG_CONTROL) & ~CTRL_SOFT_RESET;
        wr32(base, REG_CONTROL, ctrl | CTRL_SOFT_RESET);
        print_json(base);

    } else if (strcmp(argv[2], "window") == 0) {
        if (argc != 4) {
            usage(argv[0]);
            munmap(map, (size_t)page_size);
            close(fd);
            return 1;
        }
        uint32_t us = (uint32_t)strtoul(argv[3], NULL, 0);
        if (us < 1000u) us = 1000u;
        wr32(base, REG_MEAS_TIME_US, us);
        print_json(base);

    } else if (strcmp(argv[2], "trig") == 0) {
        if (argc != 4) {
            usage(argv[0]);
            munmap(map, (size_t)page_size);
            close(fd);
            return 1;
        }
        uint64_t trig_phase_step = (uint64_t)strtoull(argv[3], NULL, 0);
        wr48u(base, REG_TRIG_PHASE_STEP_LO, REG_TRIG_PHASE_STEP_HI, trig_phase_step);
        print_json(base);

    } else if (strcmp(argv[2], "osc") == 0) {
        if (argc != 6) {
            usage(argv[0]);
            munmap(map, (size_t)page_size);
            close(fd);
            return 1;
        }
        uint32_t dwell         = (uint32_t)strtoul(argv[3], NULL, 0);
        uint64_t phase_preload = (uint64_t)strtoull(argv[4], NULL, 0);
        uint32_t n_steps       = (uint32_t)strtoul(argv[5], NULL, 0);
        if (n_steps < 1u) n_steps = 1u;
        /* Write preload high word first for atomic FPGA latch */
        wr48u(base, REG_OSC_PHASE_PRELOAD_LO, REG_OSC_PHASE_PRELOAD_HI, phase_preload);
        wr32(base, REG_DWELL_CYCLES, dwell);
        wr32(base, REG_N_STEPS, n_steps);
        print_json(base);

    } else if (strcmp(argv[2], "preload") == 0) {
        if (argc != 4) {
            usage(argv[0]);
            munmap(map, (size_t)page_size);
            close(fd);
            return 1;
        }
        /* Edge-lock phase offset: set only osc_phase_preload, leaving
         * dwell_cycles untouched (it is ignored unless osc_mode is on).
         * phase_acc reloads the preload on the RISING edge of edge_lock, so
         * the caller must re-arm (drop bit5, then write with bit5=1) to apply. */
        uint64_t phase_preload = (uint64_t)strtoull(argv[3], NULL, 0);
        wr48u(base, REG_OSC_PHASE_PRELOAD_LO, REG_OSC_PHASE_PRELOAD_HI, phase_preload);
        print_json(base);

    } else {
        usage(argv[0]);
        munmap(map, (size_t)page_size);
        close(fd);
        return 1;
    }

    munmap(map, (size_t)page_size);
    close(fd);
    return 0;
}

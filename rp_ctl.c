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
 *                           bit 2 = force_high, bit 3 = harmonic_mode
 *   0x04  trig_half_period  CLK_HZ/(2*f_hz) cycles for DIO2 square wave (0=off)
 *   0x08  reg08             pulse: width_n (clock cycles)
 *                           harmonic: mult_n (bits [2:0], clamped to [1..5])
 *   0x0C  reserved
 *   0x10  status            bit 0 = busy, bit 1 = period_valid, bit 2 = period_stable,
 *                           bit 3 = timeout, bit 4 = freerun_active
 *   0x14  period            last raw measured input period (cycles)
 *   0x18  edge_cnt          edge count from last measurement window
 *   0x1C  phase_step_offset_lo  bits [31:0]  of signed 48-bit NCO offset
 *   0x20  phase_step_offset_hi  bits [47:32] of signed 48-bit NCO offset (in [15:0])
 *   0x24  phase_step_base_lo    bits [31:0]  of computed base step (read-only)
 *   0x28  phase_step_base_hi    bits [47:32] of computed base step (in [15:0])
 *   0x2C  phase_step_lo         bits [31:0]  of live phase_step (read-only)
 *   0x30  phase_step_hi         bits [47:32] of live phase_step (in [15:0])
 *   0x34  meas_time_us      measurement window in microseconds (min 1000)
 *
 * JSON output fields (same for both modes):
 *   control, harmonic_mode, force_high, width, mult_n,
 *   status, period_stable, freerun_active, meas_time_us,
 *   raw_period, edge_cnt,
 *   phase_step_offset, phase_step_base, phase_step
 *
 * Frequency conversions (use phase_step_base for accuracy):
 *   input_hz  = 124999999.0 * phase_step_base / 2^48
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
#define REG_TRIG_HALF_PERIOD      0x04
#define REG_REG08                 0x08
#define REG_STATUS                0x10
#define REG_RAW_PERIOD            0x14
#define REG_FILT_PERIOD           0x18
#define REG_PHASE_STEP_OFFSET_LO  0x1C
#define REG_PHASE_STEP_OFFSET_HI  0x20
#define REG_PHASE_STEP_BASE_LO    0x24
#define REG_PHASE_STEP_BASE_HI    0x28
#define REG_PHASE_STEP_LO         0x2C
#define REG_PHASE_STEP_HI         0x30
#define REG_MEAS_TIME_US          0x34

#define CTRL_ENABLE       0x01u
#define CTRL_SOFT_RESET   0x02u
#define CTRL_FORCE_HIGH   0x04u
#define CTRL_HARMONIC     0x08u

/* Bits the caller may pass; harmonic_mode is managed internally */
#define CTRL_USER_MASK    (CTRL_ENABLE | CTRL_FORCE_HIGH)

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
        "      control: bit 0=enable, bit 2=force_high\n"
        "  %s <base_addr> control <value>\n"
        "      Set only the control register (0=off, 1=modulated, 4=force-high/on).\n"
        "  %s <base_addr> window <microseconds>\n"
        "      e.g. 1000=1ms  10000=10ms  100000=100ms  500000=500ms  1000000=1s\n"
        "  %s <base_addr> trig <half_period_cycles>\n"
        "      DIO2 square wave: half_period = round(124999999 / (2*f_hz)). 0=off.\n"
        "      e.g. 62500000=1Hz  625000=100Hz  62500=1000Hz\n"
        "  %s <base_addr> soft_reset\n",
        prog, prog, arg3, prog, prog, prog, prog);
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

/* Write high word first so the FPGA latches the full 48-bit value atomically. */
static void wr48(volatile uint8_t *base, off_t lo_off, off_t hi_off, int64_t val) {
    uint64_t bits = (uint64_t)val & ((UINT64_C(1) << 48) - 1);
    wr32(base, hi_off, (uint32_t)((bits >> 32) & 0xFFFFu));
    wr32(base, lo_off, (uint32_t)(bits & 0xFFFFFFFFu));
}

static void print_json(volatile uint8_t *base) {
    const uint32_t control           = rd32(base, REG_CONTROL);
    const uint32_t harmonic_mode     = (control >> 3) & 0x1u;
    const uint32_t force_high        = (control >> 2) & 0x1u;
    const uint32_t reg08             = rd32(base, REG_REG08);
    const uint32_t mult_n            = (reg08 < 1u) ? 1u : (reg08 > 5u) ? 5u : reg08;
    const uint32_t trig_half_period  = rd32(base, REG_TRIG_HALF_PERIOD);
    const uint32_t status            = rd32(base, REG_STATUS);
    const uint32_t period_stable     = (status >> 2) & 0x1u;
    const uint32_t freerun_active    = (status >> 4) & 0x1u;
    const uint32_t meas_time_us      = rd32(base, REG_MEAS_TIME_US);
    const int64_t  step_offset       = rd48(base, REG_PHASE_STEP_OFFSET_LO, REG_PHASE_STEP_OFFSET_HI);
    const int64_t  step_base         = rd48(base, REG_PHASE_STEP_BASE_LO,   REG_PHASE_STEP_BASE_HI);
    const int64_t  step_live         = rd48(base, REG_PHASE_STEP_LO,        REG_PHASE_STEP_HI);

    printf("{\"control\":%u,\"harmonic_mode\":%u,\"force_high\":%u,"
           "\"width\":%u,\"mult_n\":%u,\"trig_half_period\":%u,"
           "\"status\":%u,\"period_stable\":%u,\"freerun_active\":%u,\"meas_time_us\":%u,"
           "\"raw_period\":%u,\"edge_cnt\":%u,"
           "\"phase_step_offset\":%lld,\"phase_step_base\":%lld,\"phase_step\":%lld}\n",
           control,
           harmonic_mode,
           force_high,
           reg08,
           mult_n,
           trig_half_period,
           status,
           period_stable,
           freerun_active,
           meas_time_us,
           rd32(base, REG_RAW_PERIOD),
           rd32(base, REG_FILT_PERIOD),
           (long long)step_offset,
           (long long)step_base,
           (long long)step_live);
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
            /* Clamp mult_n to [1, 5] */
            if (reg08_val < 1u) reg08_val = 1u;
            if (reg08_val > 5u) reg08_val = 5u;
            /* Disable output but keep harmonic_mode asserted during register update */
            wr32(base, REG_CONTROL, CTRL_HARMONIC);
            wr32(base, REG_REG08, reg08_val);
            wr48(base, REG_PHASE_STEP_OFFSET_LO, REG_PHASE_STEP_OFFSET_HI, phase_step_offset);
            wr32(base, REG_CONTROL, user_ctrl | CTRL_HARMONIC);
        } else {
            /* Disable output; harmonic_mode stays 0 */
            wr32(base, REG_CONTROL, 0);
            wr32(base, REG_REG08, reg08_val);
            wr48(base, REG_PHASE_STEP_OFFSET_LO, REG_PHASE_STEP_OFFSET_HI, phase_step_offset);
            wr32(base, REG_CONTROL, user_ctrl);   /* harmonic_mode bit 3 = 0 */
        }
        print_json(base);

    } else if (strcmp(argv[2], "control") == 0) {
        if (argc != 4) {
            usage(argv[0]);
            munmap(map, (size_t)page_size);
            close(fd);
            return 1;
        }
        uint32_t ctrl = (uint32_t)strtoul(argv[3], NULL, 0) & CTRL_USER_MASK;
        if (g_harmonic)
            ctrl |= CTRL_HARMONIC;
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
        uint32_t half = (uint32_t)strtoul(argv[3], NULL, 0);
        wr32(base, REG_TRIG_HALF_PERIOD, half);
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

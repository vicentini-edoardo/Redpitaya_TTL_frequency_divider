/*
 * rp_harmonic_ctl.c — Red Pitaya unified NCO helper, harmonic mode (harmonic_mode=1).
 *
 * Accesses the custom FPGA core via /dev/mem + mmap and prints all register
 * values as a single JSON object on stdout. The GUI calls this binary over SSH.
 *
 * This helper enforces harmonic_mode=1 (control bit 3) on every write so that
 * the unified bitfile operates in harmonic-generator mode.
 *
 * Compile on the board:
 *   gcc -O2 -o /root/rp_harmonic_ctl rp_harmonic_ctl.c
 *
 * Register map (base address passed as first argument, default 0x40600000):
 *   0x00  control           bit 0 = enable, bit 1 = soft_reset (self-clearing),
 *                           bit 2 = force_high, bit 3 = harmonic_mode (kept 1 by this helper)
 *   0x04  reserved
 *   0x08  width_n           harmonic multiplier in bits [2:0], range [1..5]
 *   0x0C  reserved
 *   0x10  status            bit 0 = busy, bit 1 = period_valid, bit 2 = period_stable,
 *                           bit 3 = timeout, bit 4 = freerun_active
 *   0x14  period            last raw measured input period (cycles)
 *   0x18  period_avg        reciprocal-counted period (cycles)
 *   0x1C  phase_step_offset_lo  bits [31:0]  of signed 48-bit NCO offset
 *   0x20  phase_step_offset_hi  bits [47:32] of signed 48-bit NCO offset (in [15:0])
 *   0x24  phase_step_base_lo    bits [31:0]  of computed base step (read-only)
 *   0x28  phase_step_base_hi    bits [47:32] of computed base step (in [15:0])
 *   0x2C  phase_step_lo         bits [31:0]  of live phase_step (read-only)
 *   0x30  phase_step_hi         bits [47:32] of live phase_step (in [15:0])
 *   0x34  meas_time_us      measurement window duration in microseconds
 *
 * Control register bits written by this helper:
 *   bit 0 (enable)       — from <control> arg, bit 0
 *   bit 2 (force_high)   — from <control> arg, bit 2
 *   bit 3 (harmonic_mode)— always 1 (harmonic mode)
 *
 * Output frequency: f_out = mult_n * 124999999 / period_avg + delta_f
 * NCO offset:       delta_f (Hz) = phase_step_offset * 124999999 / 2^48
 * NCO resolution:   ~0.44 mHz per LSB at 124.999999 MHz
 * Output duty:      exactly 50% (MSB of NCO accumulator)
 */

#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <fcntl.h>
#include <sys/mman.h>
#include <unistd.h>

#define REG_CONTROL               0x00
#define REG_WIDTH_N               0x08
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
#define CTRL_FORCE_HIGH   0x04u
#define CTRL_HARMONIC     0x08u   /* this helper always sets this 1 */

#define CTRL_USER_MASK    (CTRL_ENABLE | CTRL_FORCE_HIGH)

static void usage(const char *prog) {
    fprintf(stderr,
        "Usage:\n"
        "  %s <base_addr> read\n"
        "  %s <base_addr> write <mult_n> <phase_step_offset> <control>\n"
        "      mult_n: integer 1..5 (harmonic order)\n"
        "      phase_step_offset: signed 48-bit integer\n"
        "      control: bit 0=enable, bit 2=force_high (bit 3 forced 1 = harmonic mode)\n"
        "  %s <base_addr> control <value>\n"
        "      Set only the control register (bit 0=enable, bit 2=force_high).\n"
        "      Useful for Laser Off (0) or Laser On (4) without changing other regs.\n"
        "  %s <base_addr> window <microseconds>\n"
        "      e.g. 1000=1ms, 10000=10ms, 100000=100ms, 500000=500ms, 1000000=1000ms\n"
        "  %s <base_addr> soft_reset\n",
        prog, prog, prog, prog, prog);
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

static void wr48(volatile uint8_t *base, off_t lo_off, off_t hi_off, int64_t val) {
    uint64_t bits = (uint64_t)val & ((UINT64_C(1) << 48) - 1);
    wr32(base, hi_off, (uint32_t)((bits >> 32) & 0xFFFFu));
    wr32(base, lo_off, (uint32_t)(bits & 0xFFFFFFFFu));
}

static void print_json(volatile uint8_t *base) {
    const uint32_t control        = rd32(base, REG_CONTROL);
    const uint32_t harmonic_mode  = (control >> 3) & 0x1u;
    const uint32_t force_high     = (control >> 2) & 0x1u;
    const uint32_t reg08          = rd32(base, REG_WIDTH_N);
    const uint32_t mult_n         = (reg08 < 1u) ? 1u : (reg08 > 5u) ? 5u : reg08;
    const uint32_t status         = rd32(base, REG_STATUS);
    const uint32_t period_stable  = (status >> 3) & 0x1u;
    const uint32_t freerun_active = (status >> 4) & 0x1u;
    const uint32_t meas_time_us   = rd32(base, REG_MEAS_TIME_US);
    const int64_t  step_offset    = rd48(base, REG_PHASE_STEP_OFFSET_LO, REG_PHASE_STEP_OFFSET_HI);
    const int64_t  step_base      = rd48(base, REG_PHASE_STEP_BASE_LO,   REG_PHASE_STEP_BASE_HI);
    const int64_t  step_live      = rd48(base, REG_PHASE_STEP_LO,        REG_PHASE_STEP_HI);

    printf("{\"control\":%u,\"harmonic_mode\":%u,\"force_high\":%u,"
           "\"width\":%u,\"mult_n\":%u,"
           "\"status\":%u,\"period_stable\":%u,\"freerun_active\":%u,\"meas_time_us\":%u,"
           "\"raw_period\":%u,\"period_avg\":%u,"
           "\"phase_step_offset\":%lld,\"phase_step_base\":%lld,\"phase_step\":%lld}\n",
           control,
           harmonic_mode,
           force_high,
           reg08,
           mult_n,
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
        /* write <mult_n> <phase_step_offset> <control> */
        if (argc != 6) {
            usage(argv[0]);
            munmap(map, (size_t)page_size);
            close(fd);
            return 1;
        }
        uint32_t mult_n            = (uint32_t)strtoul(argv[3], NULL, 0);
        int64_t  phase_step_offset = (int64_t)strtoll(argv[4], NULL, 0);
        /* Accept enable (bit 0) and force_high (bit 2) from caller; force harmonic_mode=1 */
        uint32_t control           = ((uint32_t)strtoul(argv[5], NULL, 0) & CTRL_USER_MASK) | CTRL_HARMONIC;

        /* Clamp mult_n to [1, 5] */
        if (mult_n < 1u) mult_n = 1u;
        if (mult_n > 5u) mult_n = 5u;

        wr32(base, REG_CONTROL, CTRL_HARMONIC);   /* disable output, keep harmonic_mode */
        wr32(base, REG_WIDTH_N, mult_n);
        wr48(base, REG_PHASE_STEP_OFFSET_LO, REG_PHASE_STEP_OFFSET_HI, phase_step_offset);
        wr32(base, REG_CONTROL, control);

        print_json(base);

    } else if (strcmp(argv[2], "control") == 0) {
        /* Set only the control register (for Laser Off / Laser On override). */
        if (argc != 4) {
            usage(argv[0]);
            munmap(map, (size_t)page_size);
            close(fd);
            return 1;
        }
        /* Always keep harmonic_mode=1 */
        uint32_t ctrl = ((uint32_t)strtoul(argv[3], NULL, 0) & CTRL_USER_MASK) | CTRL_HARMONIC;
        wr32(base, REG_CONTROL, ctrl);
        print_json(base);

    } else if (strcmp(argv[2], "soft_reset") == 0) {
        const uint32_t ctrl = rd32(base, REG_CONTROL) & ~0x2u;
        wr32(base, REG_CONTROL, ctrl | 0x2u);
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

/*
 * rp_pulse_ctl.c — Red Pitaya pulse generator register control helper
 *
 * Accesses the custom FPGA core via /dev/mem + mmap and prints all register
 * values as a single JSON object on stdout. The GUI calls this binary over SSH.
 *
 * Compile on the board:
 *   gcc -O2 -o /root/rp_pulse_ctl rp_pulse_ctl.c
 *
 * Register map (base address passed as first argument, default 0x40600000):
 *   0x00  control       bit 0 = output enable, bit 1 = soft reset (self-clearing)
 *   0x04  divider       kept for address stability; unused by pulse generator
 *   0x08  width         pulse width in 125 MHz clock cycles
 *   0x0C  delay         kept for address stability; unused by pulse generator
 *   0x10  status        bit 0 = busy, bit 1 = period_valid, bit 2 = timeout,
 *                       bit 3 = period_stable, bit 4 = freerun_active
 *   0x14  period        last raw measured input period (cycles)
 *   0x18  period_avg    IIR-filtered measured input period (cycles)
 *   0x1C  period_offset signed 32-bit period offset in clock cycles
 *                       (output_period = latched_period + offset)
 *   0x20  output_period actual clamped output period in use (read-only)
 *
 * Frequency from period: freq_hz = 125000000.0 / period_cycles
 * Frequency offset:      delta_f ≈ -offset * 125e6 / period_avg^2
 */

#include <stdio.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <fcntl.h>
#include <sys/mman.h>
#include <unistd.h>

#define REG_CONTROL      0x00
#define REG_DIVIDER      0x04
#define REG_WIDTH        0x08
#define REG_DELAY        0x0C
#define REG_STATUS       0x10
#define REG_RAW_PERIOD   0x14
#define REG_FILT_PERIOD  0x18
#define REG_PERIOD_OFFSET 0x1C
#define REG_OUTPUT_PERIOD 0x20

static void usage(const char *prog) {
    fprintf(stderr,
        "Usage:\n"
        "  %s <base_addr> read\n"
        "  %s <base_addr> write <width> <period_offset> <control>\n"
        "  %s <base_addr> soft_reset\n",
        prog, prog, prog);
}

static uint32_t rd32(volatile uint8_t *base, off_t off) {
    return *(volatile uint32_t *)(base + off);
}

static void wr32(volatile uint8_t *base, off_t off, uint32_t val) {
    *(volatile uint32_t *)(base + off) = val;
}

/* Print all registers as a JSON object. The GUI parses this output. */
static void print_json(volatile uint8_t *base) {
    const uint32_t raw_period    = rd32(base, REG_RAW_PERIOD);
    const uint32_t filt_period   = rd32(base, REG_FILT_PERIOD);
    const uint32_t status        = rd32(base, REG_STATUS);
    const uint32_t period_stable = (status >> 3) & 0x1u;
    const uint32_t freerun_active = (status >> 4) & 0x1u;
    /* period_offset is a signed 32-bit value transmitted as unsigned bits */
    const int32_t  period_offset = (int32_t)rd32(base, REG_PERIOD_OFFSET);
    printf("{\"control\":%u,\"divider\":%u,\"width\":%u,\"delay\":%u,"
           "\"status\":%u,\"period_stable\":%u,\"freerun_active\":%u,"
           "\"raw_period\":%u,\"period_avg\":%u,"
           "\"period_offset\":%d,\"output_period\":%u}\n",
           rd32(base, REG_CONTROL),
           rd32(base, REG_DIVIDER),
           rd32(base, REG_WIDTH),
           rd32(base, REG_DELAY),
           status,
           period_stable,
           freerun_active,
           raw_period,
           filt_period,
           period_offset,
           rd32(base, REG_OUTPUT_PERIOD));
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

    phys = (off_t)strtoull(argv[1], NULL, 0);
    page_size = sysconf(_SC_PAGESIZE);
    /* Align to page boundary required by mmap */
    page_base = phys & ~((off_t)page_size - 1);
    page_off  = phys - page_base;

    fd = open("/dev/mem", O_RDWR | O_SYNC);
    if (fd < 0) {
        perror("open");
        return 1;
    }

    map = mmap(NULL, page_size, PROT_READ | PROT_WRITE, MAP_SHARED, fd, page_base);
    if (map == MAP_FAILED) {
        perror("mmap");
        close(fd);
        return 1;
    }

    base = (volatile uint8_t *)map + page_off;

    if (strcmp(argv[2], "read") == 0) {
        print_json(base);

    } else if (strcmp(argv[2], "write") == 0) {
        /* write <width> <period_offset> <control> */
        if (argc != 6) {
            usage(argv[0]);
            munmap((void *)((uintptr_t)base - page_off), page_size);
            close(fd);
            return 1;
        }
        uint32_t width         = (uint32_t)strtoul(argv[3], NULL, 0);
        int32_t  period_offset = (int32_t)strtol(argv[4], NULL, 0);
        uint32_t control       = (uint32_t)strtoul(argv[5], NULL, 0) & 0x1u; /* bit 0 only */

        /* Disable output before changing parameters to avoid glitches */
        wr32(base, REG_CONTROL, 0);
        wr32(base, REG_WIDTH, width);
        wr32(base, REG_PERIOD_OFFSET, (uint32_t)period_offset);
        /* bit0 = pulse_enable; bit1 (soft_reset) is self-clearing */
        wr32(base, REG_CONTROL, control);

        print_json(base);

    } else if (strcmp(argv[2], "soft_reset") == 0) {
        const uint32_t ctrl = rd32(base, REG_CONTROL) & ~0x2u;
        /* Bit 1 is self-clearing in the FPGA (single-cycle strobe).
         * Preserve bit 0 (enable). */
        wr32(base, REG_CONTROL, ctrl | 0x2u);
        print_json(base);

    } else {
        usage(argv[0]);
        munmap((void *)((uintptr_t)base - page_off), page_size);
        close(fd);
        return 1;
    }

    munmap((void *)((uintptr_t)base - page_off), page_size);
    close(fd);
    return 0;
}

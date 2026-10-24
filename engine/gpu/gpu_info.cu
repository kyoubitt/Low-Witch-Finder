#include <cstdio>
#include <cuda_runtime.h>
int main() {
    int n = 0; if (cudaGetDeviceCount(&n) != cudaSuccess || n < 1) { printf("gpu_count=0\n"); return 1; }
    cudaDeviceProp p; cudaGetDeviceProperties(&p, 0);
    int clk = 0, mclk = 0, smemOptin = 0, l2 = 0; cudaDeviceGetAttribute(&clk, cudaDevAttrClockRate, 0); cudaDeviceGetAttribute(&mclk, cudaDevAttrMemoryClockRate, 0);
    cudaDeviceGetAttribute(&smemOptin, cudaDevAttrMaxSharedMemoryPerBlockOptin, 0); cudaDeviceGetAttribute(&l2, cudaDevAttrL2CacheSize, 0);
    int rv = 0, dv = 0; cudaRuntimeGetVersion(&rv); cudaDriverGetVersion(&dv);
    size_t fr = 0, tot = 0; cudaMemGetInfo(&fr, &tot);
    printf("gpu_count=%d\ngpu_name=\"%s\"\ngpu_cc=%d.%d\ngpu_sms=%d\ngpu_clock_mhz=%d\ngpu_mem_mib=%zu\ngpu_mem_free_mib=%zu\ngpu_smem_per_sm_kib=%zu\ngpu_smem_per_block_optin_kib=%d\ngpu_l2_kib=%d\ngpu_max_threads_per_sm=%d\ngpu_regs_per_sm=%d\ncuda_runtime=%d\ncuda_driver=%d\n",
           n, p.name, p.major, p.minor, p.multiProcessorCount, clk / 1000, tot >> 20, fr >> 20, p.sharedMemPerMultiprocessor >> 10, smemOptin >> 10, l2 >> 10, p.maxThreadsPerMultiProcessor, p.regsPerMultiprocessor, rv, dv);
    return 0;
}

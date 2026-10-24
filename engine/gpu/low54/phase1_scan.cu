// GPU Phase-1 scanner: runs SearchCoords.check() + passesDensityPrefilter() (Java Phase
// 1's complete rejection pipeline) over a rectangle of swamp-hut REGIONS on the GPU,
// emitting the surviving candidates. Can batch MANY world seeds into one run: the seed
// axis is folded into the kernel's grid.y dimension, so a single kernel launch covers
// every (region, seed) pair in the batch -- this matters most when the per-seed region
// range is small (e.g. a spawn-radius search across a seed list), where a single seed's
// grid alone wouldn't have enough parallelism to fill this card's 188 SMs.
//
// An arbitrarily large seed list is handled by looping internal batches of <=65000 seeds
// (gridDim.y is hardware-capped at 65535) WITHIN one process/CUDA-context lifetime --
// earlier this was done by relaunching the whole executable per batch from the Java
// wrapper, which measured at ~12.6s per 65,000-seed batch despite only ~2.1s of that
// being host-side cache construction and ~0.03s being the actual kernel. The ~10s gap was
// unaccounted for because the cudaMalloc/cudaMemcpy upload step had no timing around it
// at all. Tracing it down: the staging buffers were plain std::vector (pageable host
// memory), which the CUDA driver has to stage-copy through an internal pinned buffer
// anyway -- typically 2-4x slower than a transfer from actually-pinned memory. Fixed by
// (a) allocating the staging buffers via cudaMallocHost instead of std::vector, and (b)
// moving the batch loop in here instead of in the Java wrapper, so a large seed list pays
// CUDA context init exactly once instead of once per batch.
//
// This replaces only Phase 1 of the Java search. Phase 2 (SeedChecker column scans and
// real chunk generation) stays on the CPU -- it needs full Minecraft worldgen.
//
// Correctness: the math here is the same code validated bit-exact against Java by
// validate_against_java.exe (2015/2015 values) and validate_density_prefilter.exe /
// validate_device_density.exe (2115/2115, 1665/1665). --self-test additionally re-checks
// the DEVICE results against the host path at runtime, per seed, so a GPU-specific
// divergence (fast-math, FMA contraction, etc.) cannot slip through unnoticed.
//
// Build:
//   nvcc -O3 -arch=sm_120 -o phase1_scan.exe phase1_scan.cu
// (-fmad=false is set below in code comments' spirit via compile flags in build.bat)
#include "device_noise.h"
#include "device_density.h"
#include "device_construct.h"
#include "swamphut.h"
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <cctype>
#include <vector>
#include <string>
#include <thread>
#include <chrono>
#include <algorithm>
#include <sstream>
#include <fstream>
#include <iostream>

#define CUDA_OK(call) do { \
    cudaError_t _e = (call); \
    if (_e != cudaSuccess) { \
        std::fprintf(stderr, "CUDA error %s at %s:%d\n", cudaGetErrorString(_e), __FILE__, __LINE__); \
        std::exit(1); \
    } \
} while (0)

// `est` carries the density prefilter's own column-top estimate out with the candidate, so
// it can be diffed against Phase 2's exact level-4 estimate. That error distribution is what
// decides whether the GPU estimate can ever replace the CPU prefilter.
struct Candidate { long long seed; int chunkX, chunkZ; float est; };

// Seed source for the three kernels that materialise a seed. Three modes:
//   --seed-list   : read seeds[idx] as given
//   --seed-gen    : splitmix64(base + idx), an arbitrary pseudo-random stream
//   --quad-bases  : sweep (quad-hut base) x (high 16 bits). Index i decomposes as
//                   base = quadBases[i >> 16], high = i & 0xffff. The quad-hut
//                   GEOMETRY lives entirely in the low 48 bits, so every one of a
//                   base's 65536 high-bit variants keeps the four huts in exactly
//                   the same place while re-rolling terrain independently
//                   (verified: one low48 sampled at a fixed position across five
//                   high-bit variants gave -55/50/79/89/73).
//                   Materialising this list on the host would be ~400 GB; here it
//                   costs one broadcast read of a 6 MB array that stays in L2,
//                   since 65536 consecutive indices share a base.
__host__ __device__ inline long long dev_seedForIndex(
        const long long* __restrict__ seeds,
        const unsigned long long* __restrict__ quadBases,
        bool useSeedGen, bool useQuadBases,
        unsigned long long genBase, int idx)
{
    if (useQuadBases) {
        unsigned long long gi = genBase + (unsigned long long)idx;
        return (long long)(quadBases[gi >> 16] | ((gi & 0xffffULL) << 48));
    }
    if (useSeedGen) return (long long)dev_splitmix64(genBase + (unsigned long long)idx);
    return seeds[idx];
}

// STAGE 0a: builds ONLY the climate trio (erosion/temperature/ridge -- ~26% of a seed's
// total sampler cost) for EVERY seed in the batch. Unconditional and unavoidable: unlike
// the density prefilter (which only ever runs after a REGION already passed dev_check, a
// clean two-stage split), climate is dev_check()'s OWN first test -- we can't know which
// seeds have ANY climate-passing region anywhere in the search area without checking, so
// this part can't be deferred any further than "cheapest 26% first, build it for
// everyone."
//
// useSeedGen mode: writes the generated seed back into seeds[idx], same as
// constructCacheKernel used to -- this is now the FIRST kernel to touch each seed, so the
// write-back moved here (climateCheckKernel right after it needs seeds[] already
// populated).
__global__ void constructClimateKernel(long long* __restrict__ seeds, int numSeeds,
                                       bool useSeedGen, unsigned long long seedGenBase,
                                       const unsigned long long* __restrict__ quadBases, bool useQuadBases,
                                       const unsigned char* __restrict__ aliveFlags,
                                       DevClimateCache* __restrict__ outClimate) {
    int idx = blockIdx.x * blockDim.x + threadIdx.x;
    if (idx >= numSeeds) return;
    long long seed = dev_seedForIndex(seeds, quadBases, useSeedGen, useQuadBases, seedGenBase, idx);
    if (useSeedGen || useQuadBases) seeds[idx] = seed;
    if (aliveFlags && !aliveFlags[idx]) return; // position-gated out, see positionGateKernel
    dev_buildClimateCache(seed, &outClimate[idx]);
}

// dev_check()'s first three conditions (erosion/temperature/ridge), shared by
// climateCheckKernel and climateCheckAllKernel so the two can never drift apart.
__device__ inline bool dev_climatePasses(const DevClimateCache& cc, int hutX, int hutZ, bool is26_2)
{
    int climateX = hutX + 8, climateZ = hutZ + 8;
    double erosionSample = dev_sampleY0(cc.erosion, climateX / 4.0, climateZ / 4.0);
    if (erosionSample < 0.55) return false;
    double temperature = dev_sampleY0(cc.temperature, climateX / 4.0, climateZ / 4.0);
    if (temperature > 0.2 || temperature < -0.45) return false;
    double ridge = dev_sampleY0(cc.ridge, climateX / 4.0, climateZ / 4.0);
    if ((ridge > 0.42 && ridge < 0.91) || (ridge < -0.42 && ridge > -0.91)) return false;
    if (is26_2 && ridge <= -0.91) return false;
    return true;
}

// Computes a region's hut position with the same LCG every other kernel uses.
__device__ inline void dev_hutPosFor(long long worldSeed, int rx, int rz,
                                     int salt, int spacing, int offset,
                                     int* outHutX, int* outHutZ)
{
    JavaRandom rand;
    long long regionSeed = (long long)rx * 341873128712LL
                         + (long long)rz * 132897987541LL
                         + worldSeed + (long long)salt;
    rand.setSeed(regionSeed);
    *outHutX = 16 * (rx * spacing + rand.nextInt(offset));
    *outHutZ = 16 * (rz * spacing + rand.nextInt(offset));
}

// ALL-REGIONS climate gate (--require-all-regions). climateCheckKernel marks a seed a
// survivor if ANY region passes climate; for a quad-hut search that is the wrong test --
// a working quad needs all four huts to exist, so a seed where only one region is swampy
// can never be one no matter how deep that hut is. (Observed directly: a -22 hit found
// under OR semantics turned out to have only 2/4 huts generating.)
//
// Climate passing is a NECESSARY condition for a hut to be viable there, so requiring it
// at every region never discards a genuine all-4-viable seed -- it only stops the pipeline
// from paying the ~74%-of-cost remaining samplers for seeds that provably cannot qualify.
// One thread per SEED (not per region) so it can early-exit on the first region that
// fails, which is the common case.
__global__ void climateCheckAllKernel(const DevClimateCache* __restrict__ climateCaches,
                                      const long long* __restrict__ seeds,
                                      int numSeeds,
                                      int minRegionX, int minRegionZ,
                                      int regionsX, int regionsZ,
                                      bool is26_2,
                                      int salt, int spacing, int offset,
                                      const unsigned char* __restrict__ aliveFlags,
                                      unsigned char* __restrict__ survivorFlags) {
    int seedIdx = blockIdx.x * blockDim.x + threadIdx.x;
    if (seedIdx >= numSeeds) return;
    if (aliveFlags && !aliveFlags[seedIdx]) return;

    long long worldSeed = seeds[seedIdx];
    const DevClimateCache& cc = climateCaches[seedIdx];

    for (int rz = 0; rz < regionsZ; rz++) {
        for (int rx = 0; rx < regionsX; rx++) {
            int hutX, hutZ;
            dev_hutPosFor(worldSeed, minRegionX + rx, minRegionZ + rz,
                          salt, spacing, offset, &hutX, &hutZ);
            if (!dev_climatePasses(cc, hutX, hutZ, is26_2)) return; // one failure kills it
        }
    }
    survivorFlags[seedIdx] = 1;
}

// STAGED CLIMATE BUILD (used with --require-all-regions). Sampler construction is ~97% of
// this tool's GPU time (measured), and constructClimateKernel was building all THREE
// climate samplers for every seed before anything could reject it. Erosion alone is the
// first test dev_check applies, so building just erosion, rejecting on it, and only then
// building temperature+ridge for the survivors cuts the dominant cost by roughly
// 3x/(1 + 2*P(all regions pass erosion)).
//
// Splitting is safe because the three samplers are independently keyed derivations of the
// seed -- see dev_buildClimateErosion's comment.
__global__ void constructErosionKernel(long long* __restrict__ seeds, int numSeeds,
                                       bool useSeedGen, unsigned long long seedGenBase,
                                       const unsigned long long* __restrict__ quadBases, bool useQuadBases,
                                       const unsigned char* __restrict__ aliveFlags,
                                       DevClimateCache* __restrict__ outClimate) {
    int idx = blockIdx.x * blockDim.x + threadIdx.x;
    if (idx >= numSeeds) return;
    long long seed = dev_seedForIndex(seeds, quadBases, useSeedGen, useQuadBases, seedGenBase, idx);
    if (useSeedGen || useQuadBases) seeds[idx] = seed;
    if (aliveFlags && !aliveFlags[idx]) return;
    dev_buildClimateErosion(seed, &outClimate[idx]);
}

// ALL-regions erosion gate: one thread per seed, early-exit on the first region whose
// erosion fails. Erosion passing everywhere is necessary (not sufficient) for the full
// climate test to pass everywhere, so this never discards a seed the old path would keep.
__global__ void erosionCheckAllKernel(const DevClimateCache* __restrict__ climateCaches,
                                      const long long* __restrict__ seeds, int numSeeds,
                                      int minRegionX, int minRegionZ,
                                      int regionsX, int regionsZ,
                                      int salt, int spacing, int offset,
                                      const unsigned char* __restrict__ aliveFlags,
                                      unsigned char* __restrict__ erosionFlags) {
    int seedIdx = blockIdx.x * blockDim.x + threadIdx.x;
    if (seedIdx >= numSeeds) return;
    if (aliveFlags && !aliveFlags[seedIdx]) return;
    long long worldSeed = seeds[seedIdx];
    const DevClimateCache& cc = climateCaches[seedIdx];
    for (int rz = 0; rz < regionsZ; rz++) {
        for (int rx = 0; rx < regionsX; rx++) {
            int hutX, hutZ;
            dev_hutPosFor(worldSeed, minRegionX + rx, minRegionZ + rz, salt, spacing, offset, &hutX, &hutZ);
            double e = dev_sampleY0(cc.erosion, (hutX + 8) / 4.0, (hutZ + 8) / 4.0);
            if (e < 0.55) return;
        }
    }
    erosionFlags[seedIdx] = 1;
}

// Builds ONLY temperature+ridge, for the erosion survivors.
__global__ void constructTempRidgeKernel(const long long* __restrict__ seeds, int numSeeds,
                                         const unsigned char* __restrict__ erosionFlags,
                                         DevClimateCache* __restrict__ outClimate) {
    int idx = blockIdx.x * blockDim.x + threadIdx.x;
    if (idx >= numSeeds) return;
    if (!erosionFlags[idx]) return;
    dev_buildClimateTempRidge(seeds[idx], &outClimate[idx]);
}

// STAGE -1 (optional, --max-hut-dist): the CHEAPEST possible filter, run before ANY sampler
// is built. A hut's position depends only on the region LCG (and so only on the seed's low
// 48 bits) -- no noise, no climate, ~5 LCG steps. When the goal is "a hut near a specific
// point" rather than "a hut anywhere in the search box", almost every seed places its huts
// too far away to ever matter, and without this gate every one of them still pays the full
// unconditional climate build in constructClimateKernel (~26% of a seed's sampler cost)
// before anything discovers that.
//
// Marks a seed alive if ANY region in the search area puts a hut within maxDist blocks of
// (centerX, centerZ). Same non-atomic same-value store as climateCheckKernel -- region
// threads racing to set the same seed's flag to 1 can't disagree.
//
// Distance is measured to 16*chunkX / 16*chunkZ, i.e. the exact coordinate reported as the
// hit position, so --max-hut-dist N means "the reported /tp is within N blocks of center".
//
// Generates its own seed under --seed-gen (same splitmix64 formula constructClimateKernel
// uses) rather than depending on a prior kernel to populate seeds[] -- keeps this strictly
// first in the pipeline with no ordering hazard.
__global__ void positionGateKernel(const long long* __restrict__ seeds, int numSeeds,
                                   bool useSeedGen, unsigned long long seedGenBase,
                                   const unsigned long long* __restrict__ quadBases, bool useQuadBases,
                                   int minRegionX, int minRegionZ,
                                   int regionsX, int regionsZ,
                                   int salt, int spacing, int offset,
                                   int centerX, int centerZ, long long maxDistSq,
                                   unsigned char* __restrict__ aliveFlags) {
    long long idx = (long long)blockIdx.x * blockDim.x + threadIdx.x;
    long long total = (long long)regionsX * regionsZ;
    if (idx >= total) return;

    int seedIdx = blockIdx.y;
    if (seedIdx >= numSeeds) return;
    if (aliveFlags[seedIdx]) return; // some other region already qualified this seed

    long long worldSeed = dev_seedForIndex(seeds, quadBases, useSeedGen, useQuadBases, seedGenBase, seedIdx);

    int rx = minRegionX + (int)(idx % regionsX);
    int rz = minRegionZ + (int)(idx / regionsX);

    // Identical placement LCG to checkKernel/climateCheckKernel -- must stay in lockstep
    // with them or the gate would reject positions those kernels would actually check.
    JavaRandom rand;
    long long regionSeed = (long long)rx * 341873128712LL
                         + (long long)rz * 132897987541LL
                         + worldSeed + (long long)salt;
    rand.setSeed(regionSeed);
    int chunkX = rx * spacing + rand.nextInt(offset);
    int chunkZ = rz * spacing + rand.nextInt(offset);

    long long dx = (long long)16 * chunkX - centerX;
    long long dz = (long long)16 * chunkZ - centerZ;
    if (dx * dx + dz * dz <= maxDistSq) aliveFlags[seedIdx] = 1;
}

// STAGE 0b: dev_check()'s first three conditions ONLY (erosion/temperature/ridge), over
// every (region, seed) pair, using JUST the climate cache stage 0a built. Marks any seed
// with at least one climate-passing region as a survivor -- a seed that fails climate on
// EVERY region in the search area can never pass dev_check() anywhere (climate is checked
// before anything else in dev_check, see below), so building its remaining ~74%-of-cost
// samplers at all would be pure waste. The write is a plain (non-atomic) store: multiple
// region-threads for the same seed racing to set the SAME seed's flag to the SAME value
// (1) is safe -- there's no scenario where two threads disagree about a shared seed's
// outcome, so no read-modify-write is needed.
__global__ void climateCheckKernel(const DevClimateCache* __restrict__ climateCaches,
                                   const long long* __restrict__ seeds,
                                   int minRegionX, int minRegionZ,
                                   int regionsX, int regionsZ,
                                   bool is26_2,
                                   int salt, int spacing, int offset,
                                   int gateCenterX, int gateCenterZ, long long gateMaxDistSq,
                                   const unsigned char* __restrict__ aliveFlags,
                                   unsigned char* __restrict__ survivorFlags) {
    long long idx = (long long)blockIdx.x * blockDim.x + threadIdx.x;
    long long total = (long long)regionsX * regionsZ;
    if (idx >= total) return;

    int seedIdx = blockIdx.y;
    // MUST come before touching climateCaches[seedIdx]: constructClimateKernel skips
    // position-gated-out seeds, so their cache slot holds stale/uninitialized data from
    // whatever batch last used this pipeline slot. Sampling it would set survivorFlags on
    // garbage and let a bogus candidate through the rest of the pipeline.
    if (aliveFlags && !aliveFlags[seedIdx]) return;
    if (survivorFlags[seedIdx]) return; // already known to survive -- skip redundant sampling

    long long worldSeed = seeds[seedIdx];
    const DevClimateCache& cc = climateCaches[seedIdx];

    int rx = minRegionX + (int)(idx % regionsX);
    int rz = minRegionZ + (int)(idx / regionsX);

    // Same LCG hut placement as checkKernel -- must match exactly, since climate is
    // sampled at the PLACED hut's coordinates, not the region's.
    JavaRandom rand;
    long long regionSeed = (long long)rx * 341873128712LL
                         + (long long)rz * 132897987541LL
                         + worldSeed + (long long)salt;
    rand.setSeed(regionSeed);
    int chunkX = rx * spacing + rand.nextInt(offset);
    int chunkZ = rz * spacing + rand.nextInt(offset);
    int hutX = 16 * chunkX, hutZ = 16 * chunkZ;

    // Per-region position gate. The seed-level aliveFlags only says SOME region qualified;
    // this rejects the individual regions that didn't, before any sampling.
    if (gateMaxDistSq >= 0) {
        long long gdx = (long long)hutX - gateCenterX;
        long long gdz = (long long)hutZ - gateCenterZ;
        if (gdx * gdx + gdz * gdz > gateMaxDistSq) return;
    }

    // Mirrors dev_check()'s first three conditions exactly (device_noise.h).
    int climateX = hutX + 8, climateZ = hutZ + 8;
    double erosionSample = dev_sampleY0(cc.erosion, climateX / 4.0, climateZ / 4.0);
    if (erosionSample < 0.55) return;
    double temperature = dev_sampleY0(cc.temperature, climateX / 4.0, climateZ / 4.0);
    if (temperature > 0.2 || temperature < -0.45) return;
    double ridge = dev_sampleY0(cc.ridge, climateX / 4.0, climateZ / 4.0);
    if ((ridge > 0.42 && ridge < 0.91) || (ridge < -0.42 && ridge > -0.91)) return;
    if (is26_2 && ridge <= -0.91) return;

    survivorFlags[seedIdx] = 1;
}

// grid.y selects the seed within the batch -- gridDim.y (and so the seed count per
// launch) is hardware-capped at 65535 on this and every other CC>=3.0 device.
//
// STAGE 1 of 2 (see densityFilterKernel below): only dev_check() -- the density prefilter
// used to run unconditionally right here, which meant building a FULL DevDensityContext
// (shift/jagged/terrain noise, ~53% of a seed's total construction cost -- see
// constructCacheKernel's comment) for every seed in the batch, even though at a typical
// search radius roughly 1 in 1,000,000 seeds ever reaches this check at all. Deferred to a
// second kernel that only builds density contexts for the (typically tiny) list of
// survivors THIS kernel emits (into the SAME Candidate shape stage 2 also emits -- stage 1's
// output needs nothing beyond seed/chunkX/chunkZ since stage 2 rebuilds everything else it
// needs straight from the seed, see densityFilterKernel's comment).
//
// survivorFlags gate: caches[seedIdx] was only built (by constructCacheKernel) for seeds
// climateCheckKernel marked as survivors -- for anyone else it's stale/uninitialized data
// left over from whatever THIS pipeline slot last held, so this MUST check the flag before
// touching caches[seedIdx] at all, not just as a redundant fast-path.
__global__ void checkKernel(const DevNoiseCache* __restrict__ caches,
                             const long long* __restrict__ seeds,
                             const unsigned char* __restrict__ survivorFlags,
                             int minRegionX, int minRegionZ,
                             int regionsX, int regionsZ,
                             int maxHeight, bool is26_2,
                             int salt, int spacing, int offset,
                             int gateCenterX, int gateCenterZ, long long gateMaxDistSq,
                             Candidate* __restrict__ out,
                             unsigned int* __restrict__ outCount,
                             unsigned int outCapacity) {
    long long idx = (long long)blockIdx.x * blockDim.x + threadIdx.x;
    long long total = (long long)regionsX * regionsZ;
    if (idx >= total) return;

    int seedIdx = blockIdx.y;
    if (!survivorFlags[seedIdx]) return;

    long long worldSeed = seeds[seedIdx];
    const DevNoiseCache& cache = caches[seedIdx];

    int rx = minRegionX + (int)(idx % regionsX);
    int rz = minRegionZ + (int)(idx / regionsX);

    // Per-region hut placement (LCG) -- validated 125/125 against Java.
    JavaRandom rand;
    long long regionSeed = (long long)rx * 341873128712LL
                         + (long long)rz * 132897987541LL
                         + worldSeed + (long long)salt;
    rand.setSeed(regionSeed);
    int chunkX = rx * spacing + rand.nextInt(offset);
    int chunkZ = rz * spacing + rand.nextInt(offset);

    int hutX = 16 * chunkX;
    int hutZ = 16 * chunkZ;

    // Per-region position gate -- see climateCheckKernel's copy. Must stay before
    // dev_check: rejecting here is the entire point (dev_check is the expensive part), and
    // without it an alive seed could still emit a candidate from a far-away region.
    if (gateMaxDistSq >= 0) {
        long long gdx = (long long)hutX - gateCenterX;
        long long gdz = (long long)hutZ - gateCenterZ;
        if (gdx * gdx + gdz * gdz > gateMaxDistSq) return;
    }

    if (!dev_check(cache, hutX, hutZ, maxHeight, is26_2)) return;

    unsigned int slot = atomicAdd(outCount, 1u);
    if (slot < outCapacity) {
        out[slot].seed = worldSeed;
        out[slot].chunkX = chunkX;
        out[slot].chunkZ = chunkZ;
        out[slot].est = 0.0f;   // stage 1 has no density estimate yet; stage 2 fills it in
    }
}

// STAGE 2 of 2: one thread per dev_check() survivor -- applies dev_passesDensityPrefilter,
// matching Java's Phase 1 exactly ("check() survivors additionally get
// passesDensityPrefilter'd before being handed to Phase 2" -- measured on real GPU output
// this rejects ~67% of check() survivors). Launched ONCE per --worker request (not per
// sub-batch) over ALL survivors accumulated across the whole request, so this only adds one
// extra kernel launch + one small device->host readback per Java iteration, not 62 of them.
//
// Rebuilds BOTH the DevNoiseCache and DevDensityContext fresh, right here, from just the
// seed -- rather than looking up the noise cache constructCacheKernel already built during
// stage 1. That per-batch cache lives in one of only PIPELINE_DEPTH=2 rotating slots and is
// long overwritten by later batches by the time this runs (once, at the very end of the
// whole request) -- so re-deriving it is a correctness requirement here, not just the
// simpler option. It's also cheap: survivors are rare enough (this is the whole point of
// deferring density-context construction) that redoing the noise-cache build for the few
// that reach this stage costs nothing next to what NOT building density contexts for every
// non-survivor already saves.
//
// scratchCaches/scratchDensities: per-thread scratch space in GLOBAL memory, sized to
// EXACTLY *stage1Count by the caller right before this launch -- NOT local/stack variables.
// DevNoiseCache alone is ~127KB; a local variable that size blows CUDA's default per-thread
// stack budget and corrupts memory (hit this directly: "illegal memory access" surfacing on
// the next sync after this kernel). This is the exact same class of bug the
// dev_buildWorldNoiseCache output-pointer refactor fixed earlier this session for
// constructCacheKernel -- same lesson, this kernel just hadn't been written yet when that
// fix landed.
__global__ void densityFilterKernel(const Candidate* __restrict__ stage1,
                                    const unsigned int* __restrict__ stage1Count,
                                    int maxHeight,
                                    const DevSplineTable* __restrict__ sharedTerrainParams,
                                    DevNoiseCache* __restrict__ scratchCaches,
                                    DevDensityContext* __restrict__ scratchDensities,
                                    Candidate* __restrict__ out,
                                    unsigned int* __restrict__ outCount,
                                    unsigned int outCapacity) {
    unsigned int i = blockIdx.x * blockDim.x + threadIdx.x;
    if (i >= *stage1Count) return;
    Candidate c = stage1[i];

    DevNoiseCache& cache = scratchCaches[i];
    dev_buildWorldNoiseCache(c.seed, &cache);

    DevDensityContext& densityCtx = scratchDensities[i];
    DevDoublePerlin shift, jagged;
    DevInterpolatedNoiseSampler terrain;
    dev_buildDensityContextSeeded(c.seed, &shift, &jagged, &terrain);
    densityCtx.shiftNoise = shift;
    densityCtx.jaggedNoise = jagged;
    densityCtx.terrainNoise = terrain;
    densityCtx.terrainParams = *sharedTerrainParams;

    int hutX = 16 * c.chunkX, hutZ = 16 * c.chunkZ;
    float est = 0.0f;
    if (!dev_passesDensityPrefilter(cache, densityCtx, c.seed, hutX, hutZ, maxHeight, &est)) return;

    unsigned int slot = atomicAdd(outCount, 1u);
    if (slot < outCapacity) {
        out[slot].seed = c.seed;
        out[slot].chunkX = c.chunkX;
        out[slot].chunkZ = c.chunkZ;
        out[slot].est = est;
    }
}

// One thread per seed: builds the REMAINING 11 samplers (~74% of a seed's total sampler
// cost) directly on the GPU -- ONLY for seeds climateCheckKernel marked as survivors.
// Non-survivors return immediately, leaving outCaches[idx] untouched (checkKernel's
// survivorFlags gate means nothing ever reads it for them). Replaces what used to be
// host-side construction + a 13+GB H2D transfer per batch (see device_construct.h and
// validate_device_construct.exe, 19,640,500/19,640,500 bit-exact against the proven host
// path). Density-context construction is NOT done here either -- see densityFilterKernel's
// comment above; only checkKernel's (typically tiny) survivor list needs one built.
//
// seeds[] is read-only here -- constructClimateKernel is now the first kernel to touch
// each seed (including --seed-gen's write-back), so by the time this runs seeds[idx] is
// already populated for every seed in the batch, survivor or not.
__global__ void constructCacheKernel(const long long* __restrict__ seeds, int numSeeds,
                                     const unsigned char* __restrict__ survivorFlags,
                                     const DevClimateCache* __restrict__ climateCaches,
                                     DevNoiseCache* __restrict__ outCaches) {
    int idx = blockIdx.x * blockDim.x + threadIdx.x;
    if (idx >= numSeeds) return;
    if (!survivorFlags[idx]) return;
    long long seed = seeds[idx];

    // Climate trio already built (that's WHY this seed is a survivor) -- copy it in
    // instead of rebuilding.
    outCaches[idx].erosion = climateCaches[idx].erosion;
    outCaches[idx].temperature = climateCaches[idx].temperature;
    outCaches[idx].ridge = climateCaches[idx].ridge;

    dev_buildWorldNoiseCacheRemaining(seed, &outCaches[idx]);
}

// UNGATED construct: builds all 14 samplers for every seed in one pass, no climate gate.
// Used when climate-gating would COST rather than save -- see runScan's useClimateGate
// decision. Wide searches check so many regions per seed that nearly every seed has some
// climate-passing region somewhere, so the gate's extra climate pass buys almost no
// skipped work while still paying for itself in full (measured: ~25% slower at 400
// regions/seed). This is exactly the pre-climate-gating construct path, kept intact so the
// wide case is bit-identical AND performance-identical to before that change.
__global__ void constructFullCacheKernel(long long* __restrict__ seeds, int numSeeds,
                                         bool useSeedGen, unsigned long long seedGenBase,
                                         const unsigned long long* __restrict__ quadBases, bool useQuadBases,
                                         const unsigned char* __restrict__ aliveFlags,
                                         DevNoiseCache* __restrict__ outCaches) {
    int idx = blockIdx.x * blockDim.x + threadIdx.x;
    if (idx >= numSeeds) return;
    long long seed = dev_seedForIndex(seeds, quadBases, useSeedGen, useQuadBases, seedGenBase, idx);
    if (useSeedGen || useQuadBases) seeds[idx] = seed;
    if (aliveFlags && !aliveFlags[idx]) return; // position-gated out
    dev_buildWorldNoiseCache(seed, &outCaches[idx]);
}

// Forward declarations -- defined further down (readSeedList right after usage(), same as
// before; selfTestOneRegion right after that), but the new parseScanArgs/runScan/runWorker
// functions inserted just below usage() call both.
static bool readSeedList(const char* path, std::vector<long long>& out);
struct WorldNoiseCache; struct DensityContext; struct SwampHutConfig;
static bool selfTestOneRegion(const WorldNoiseCache& hc, const DensityContext& hd,
                              long long seed, int rx, int rz, int maxHeight, bool is26_2,
                              bool useDensityPrefilter, const SwampHutConfig& cfg,
                              const std::vector<long long>& hits,
                              int* outRx, int* outRz, int* outChunkX, int* outChunkZ,
                              bool* outDevPass, bool* outHostPass);

static void usage() {
    std::printf(
        "usage: phase1_scan --seed <n> [--seed <n> ...] [--seed-list <file>] [options]\n"
        "       phase1_scan --seed-gen <base> <count> [options]\n"
        "       phase1_scan --worker\n"
        "  --seed <n>            world seed (repeatable -- batches into one run)\n"
        "  --seed-list <file>    additional seeds, whitespace/comma-separated in a file\n"
        "  --seed-gen <base> <count>  generate <count> world seeds directly on the GPU via\n"
        "                        splitmix64(base + i) instead of uploading a seed list --\n"
        "                        skips the host-side generate/write/read/upload round trip\n"
        "                        entirely. <base> may be negative (parsed as the same 64-bit\n"
        "                        bit pattern via strtoull). Cannot combine with --seed/--seed-list.\n"
        "  --min-region-x <n>    default -128\n"
        "  --max-region-x <n>    default  128   (exclusive)\n"
        "  --min-region-z <n>    default -128\n"
        "  --max-region-z <n>    default  128   (exclusive)\n"
        "  --max-height <n>      hut height filter, default -40\n"
        "  --v26-2               use GameVersion 26.2 ridge rule\n"
        "  --out <file>          write candidates as CSV (seed,chunkX,chunkZ,blockX,blockZ)\n"
        "  --self-test           verify device results against the validated host path\n"
        "  --no-density-prefilter  skip passesDensityPrefilter (emits check()'s superset, like before)\n"
        "  --climate-gate / --no-climate-gate  force the climate pre-pass on/off instead of\n"
        "                        auto-deciding from regions-per-seed. Gating wins big on\n"
        "                        NARROW areas (1.68x at 9 regions/seed) and loses on wide\n"
        "                        ones (~25% at 400); auto picks by area, these override it.\n"
        "                        Both settings produce identical candidates either way.\n"
        "  --block <n>           threads per block, default 128\n"
        "  --worker              keep one CUDA context alive and read scan requests from\n"
        "                        stdin, one per line -- each line is the SAME flags as a\n"
        "                        normal invocation (minus --worker itself), by construction.\n"
        "                        Prints ===WORKER_READY=== once buffers are allocated, then\n"
        "                        ===SCAN_DONE rc=N=== after each line's scan (or\n"
        "                        ===SCAN_ERROR: <msg>=== for a bad line, which does NOT stop\n"
        "                        the worker). Exits cleanly on stdin EOF.\n"
        "\n"
        "The seed axis rides on the kernel's grid.y dimension, hardware-capped at 65535 per\n"
        "launch -- an arbitrarily large --seed-list is handled by looping internal batches\n"
        "of <=65000 seeds within this ONE process/CUDA-context (no per-batch relaunch, see\n"
        "the top-of-file comment). Each batch's noise caches are built directly on the GPU\n"
        "(constructCacheKernel) -- only the raw seed values themselves are ever uploaded.\n");
}

// One scan request's worth of parameters -- exactly what the CLI flags describe, whether
// they came from argv or one line of --worker's stdin. Kept as fresh defaults per call so
// a worker line that omits a flag gets the same default a normal invocation would.
struct ScanParams {
    std::vector<long long> seeds;
    bool useSeedGen = false;
    unsigned long long seedGenBase = 0;
    int seedGenCount = 0;
    int minRX = -128, maxRX = 128, minRZ = -128, maxRZ = 128;
    int maxHeight = -40;
    bool is26_2 = false, selfTest = false, noDensityPrefilter = false;
    // Climate gating (see constructClimateKernel): auto-decided from regions-per-seed
    // unless forced. 0 = auto, 1 = force on, -1 = force off. Forcing exists mainly so the
    // two paths can be A/B'd against each other for equivalence and timing.
    int climateGateMode = 0;
    int block = 128;
    // Position gate (see positionGateKernel). maxHutDist < 0 disables it entirely, which is
    // the default -- so every existing invocation keeps its exact previous behaviour and
    // this only ever narrows results when explicitly asked for.
    // --quad-bases: sweep (quad-hut base) x (high 16 bits) instead of a random
    // seed stream. Region bounds should be [-1,1)^2 so the four scanned regions are
    // exactly the quad constellation after moveStructure(-1,-1).
    std::string quadBasesPath;
    std::vector<unsigned long long> quadBases;
    bool useQuadBases = false;
    // --require-all-regions: a seed survives only if EVERY region in the box passes
    // climate, not just one. Correct semantics for quad-structure searches.
    bool requireAllRegions = false;
    long long quadBaseOffset = 0;
    long long quadBaseLimit  = 0;
    long long maxHutDist = -1;
    int gateCenterX = 0, gateCenterZ = 0;
    std::string outPath;
};

// Parses either argv[1..] (normal mode) or one tokenized --worker stdin line into a fresh
// ScanParams. Returns false (and prints to stderr) on a bad/incomplete line -- callers
// decide whether that's fatal (normal mode: yes) or just skip-this-line (worker mode: no).
static bool parseScanArgs(const std::vector<std::string>& tokens, ScanParams& p, bool isWorkerLine) {
    (void)isWorkerLine; // kept in the signature for callers' clarity at the call site
    bool ok = true;
    size_t i = 0;
    auto next = [&](const char* what) -> std::string {
        if (i + 1 >= tokens.size()) {
            std::fprintf(stderr, "missing value for %s\n", what);
            ok = false;
            return std::string();
        }
        return tokens[++i];
    };
    for (; i < tokens.size() && ok; i++) {
        const std::string& a = tokens[i];
        if (a == "--seed") { p.seeds.push_back((long long)std::strtoull(next("--seed").c_str(), nullptr, 10)); }
        else if (a == "--seed-list") {
            std::string path = next("--seed-list");
            if (ok && !readSeedList(path.c_str(), p.seeds)) {
                std::fprintf(stderr, "cannot open seed list %s\n", path.c_str());
                ok = false;
            }
        }
        else if (a == "--seed-gen") {
            std::string baseStr = next("--seed-gen (base)");
            std::string countStr = ok ? next("--seed-gen (count)") : std::string();
            if (ok) {
                p.useSeedGen = true;
                // strtoull on a negative decimal string correctly recovers the same 64-bit
                // bit pattern a Java `long` of that value would have (two's-complement
                // negation performed in the unsigned domain, per the C standard) -- no
                // separate sign-handling needed.
                p.seedGenBase = std::strtoull(baseStr.c_str(), nullptr, 10);
                p.seedGenCount = std::atoi(countStr.c_str());
            }
        }
        else if (a == "--min-region-x") p.minRX = std::atoi(next("--min-region-x").c_str());
        else if (a == "--max-region-x") p.maxRX = std::atoi(next("--max-region-x").c_str());
        else if (a == "--min-region-z") p.minRZ = std::atoi(next("--min-region-z").c_str());
        else if (a == "--max-region-z") p.maxRZ = std::atoi(next("--max-region-z").c_str());
        else if (a == "--max-height")  p.maxHeight = std::atoi(next("--max-height").c_str());
        else if (a == "--v26-2")       p.is26_2 = true;
        else if (a == "--self-test")   p.selfTest = true;
        else if (a == "--no-density-prefilter") p.noDensityPrefilter = true;
        else if (a == "--require-all-regions") p.requireAllRegions = true;
        else if (a == "--quad-bases")   p.quadBasesPath = next("--quad-bases");
        else if (a == "--quad-base-offset") p.quadBaseOffset = std::atoll(next("--quad-base-offset").c_str());
        else if (a == "--quad-base-limit")  p.quadBaseLimit  = std::atoll(next("--quad-base-limit").c_str());
        else if (a == "--max-hut-dist")  p.maxHutDist = std::atoll(next("--max-hut-dist").c_str());
        else if (a == "--gate-center-x") p.gateCenterX = std::atoi(next("--gate-center-x").c_str());
        else if (a == "--gate-center-z") p.gateCenterZ = std::atoi(next("--gate-center-z").c_str());
        else if (a == "--climate-gate")    p.climateGateMode = 1;
        else if (a == "--no-climate-gate") p.climateGateMode = -1;
        else if (a == "--block")       p.block = std::atoi(next("--block").c_str());
        else if (a == "--out")         p.outPath = next("--out");
        else if (a == "--worker")      { /* consumed by the caller before tokenizing; harmless if present */ }
        else { std::fprintf(stderr, "unknown argument: %s\n", a.c_str()); ok = false; }
    }
    if (!ok) return false;

    // --quad-bases: load the base list, then slice it. numSeeds is an int throughout the
    // batch loop, so one request must stay under 2^31 seeds -- at 65536 variants per base
    // that caps a request at 32767 bases. The caller sweeps the full list by advancing
    // --quad-base-offset, which keeps the 64-bit global index in seedGenBase where it
    // belongs and avoids widening the whole loop to 64-bit.
    if (!p.quadBasesPath.empty()) {
        std::ifstream bf(p.quadBasesPath);
        if (!bf) { std::fprintf(stderr, "cannot open --quad-bases %s\n", p.quadBasesPath.c_str()); return false; }
        unsigned long long v;
        while (bf >> v) p.quadBases.push_back(v);
        if (p.quadBases.empty()) { std::fprintf(stderr, "--quad-bases file had no seeds\n"); return false; }
        if (p.quadBaseOffset < 0 || (size_t)p.quadBaseOffset >= p.quadBases.size()) {
            std::fprintf(stderr, "--quad-base-offset out of range\n"); return false;
        }
        long long avail = (long long)p.quadBases.size() - p.quadBaseOffset;
        if (p.quadBaseLimit <= 0 || p.quadBaseLimit > avail) p.quadBaseLimit = avail;
        if (p.quadBaseLimit > 32767) {
            std::fprintf(stderr, "--quad-base-limit capped at 32767 (int seed-count limit)\n");
            p.quadBaseLimit = 32767;
        }
        p.useQuadBases = true;
        p.useSeedGen = false;
        p.seedGenBase = (unsigned long long)p.quadBaseOffset * 65536ULL;
        p.seedGenCount = (int)(p.quadBaseLimit * 65536LL);
    }

    if (p.useSeedGen && !p.seeds.empty()) {
        std::fprintf(stderr, "--seed-gen cannot be combined with --seed/--seed-list\n");
        return false;
    }
    if (p.useSeedGen && p.seedGenCount <= 0) {
        std::fprintf(stderr, "--seed-gen count must be positive\n");
        return false;
    }
    if (!p.useSeedGen && !p.useQuadBases && p.seeds.empty()) { std::fprintf(stderr, "no seeds given\n"); return false; }
    if (p.maxRX <= p.minRX || p.maxRZ <= p.minRZ) { std::fprintf(stderr, "empty region range\n"); return false; }

#ifdef DEV_Y0_ASSUME_NO_WRAP
    // The y=0 climate path drops maintainPrecision because it provably cannot wrap inside
    // the vanilla world border (see device_noise.h). That proof is a statement about the
    // COORDINATE RANGE, so verify the requested range actually respects it rather than
    // trusting the caller -- a wider scan would silently produce wrong noise otherwise.
    // Worst case is continentalness: max freq 2^-1, input climateX/4, second sampler
    // scaled by 1.0181268882175227.
    {
        long long maxAbsRegion = std::max(std::max(std::llabs((long long)p.minRX), std::llabs((long long)p.maxRX)),
                                          std::max(std::llabs((long long)p.minRZ), std::llabs((long long)p.maxRZ)));
        double maxAbsHut   = (double)(maxAbsRegion + 1) * 512.0 + 8.0;
        double maxAbsInput = maxAbsHut / 4.0 * 1.0181268882175227 * 0.5;
        if (maxAbsInput >= DN_Y0_WRAP_LIMIT) {
            std::fprintf(stderr,
                "region range too wide for DEV_Y0_ASSUME_NO_WRAP: |coord| reaches %.4g, limit %.4g.\n"
                "Rebuild without -DDEV_Y0_ASSUME_NO_WRAP, or narrow the region range.\n",
                maxAbsInput, (double)DN_Y0_WRAP_LIMIT);
            return false;
        }
    }
#endif
    return true;
}

// Everything the scan needs that's allocated ONCE and reused across every scan in this
// process's lifetime -- one process/CUDA-context for the whole run, whether that's a
// single classic invocation or arbitrarily many --worker requests. Buffers are always
// sized at MAX_SEEDS_PER_BATCH (the gridDim.y hardware cap) regardless of any one
// request's actual seed count, so a worker never needs to reallocate between requests of
// different sizes. The cap is lowered in allocateBuffers() to fit the free GPU memory;
// the full 65000 needs several GB (a large GPU has no trouble, a 6 GB one does).
static int MAX_SEEDS_PER_BATCH = 65000; // gridDim.y hardware cap is 65535; leave headroom. allocateBuffers() lowers it on GPUs with little free memory.

// Two-deep ping-pong pipeline: constructCacheKernel for sub-batch N+1 overlaps with
// checkKernel (the actual candidate check) for sub-batch N on a separate stream, instead
// of the previous fully-serialized cudaDeviceSynchronize()-after-every-kernel approach.
// Measured need for this: constructCacheKernel (one thread builds a WHOLE seed's noise
// cache -- MD5-keyed Xoroshiro128++ derivation + Perlin permutation tables, heavy
// per-thread state) costs MORE GPU time per 65,000-seed sub-batch than checkKernel
// itself (~0.111s vs ~0.066s measured), so this card -- 188 SMs, 96 GB, easily enough
// headroom to run both kernels concurrently without contention -- was sitting on real,
// free overlap the whole time. Each slot gets its own stream + device buffers so the two
// kernel families never share memory that's still in flight.
static const int PIPELINE_DEPTH = 2;
struct WorkerBuffers {
    long long* seedStaging[PIPELINE_DEPTH] = {};
    DevNoiseCache* devCaches[PIPELINE_DEPTH] = {};
    long long* devSeeds[PIPELINE_DEPTH] = {};
    // Per-slot: climate cache is built for every seed in a batch, and the survivor flag
    // array gates both constructCacheKernel and checkKernel for that same batch -- both
    // reused/overwritten every PIPELINE_DEPTH batches exactly like devCaches/devSeeds, so
    // they follow the same per-slot lifetime.
    DevClimateCache* devClimateCache[PIPELINE_DEPTH] = {};
    unsigned char* devSurvivorFlags[PIPELINE_DEPTH] = {};
    unsigned char* devAliveFlags[PIPELINE_DEPTH] = {};   // position gate, see positionGateKernel
    unsigned char* devErosionFlags[PIPELINE_DEPTH] = {};  // staged climate build, see constructErosionKernel
    // --quad-bases: whole base list lives on device for the life of the process (6 MB
    // for 755k bases). NOT per-slot -- it is read-only and identical for every batch.
    unsigned long long* devQuadBases = nullptr;
    std::string quadBasesLoadedPath;
    cudaStream_t streams[PIPELINE_DEPTH] = {};
    // Per-slot timing events -- read out only once that slot's stream is known to have
    // finished (either on reuse, PIPELINE_DEPTH iterations later, or the final drain), so
    // reading them never forces the pipeline to stall.
    cudaEvent_t evBatchStart[PIPELINE_DEPTH] = {};
    cudaEvent_t evUploadDone[PIPELINE_DEPTH] = {};
    cudaEvent_t evConstructDone[PIPELINE_DEPTH] = {};
    cudaEvent_t evCheckDone[PIPELINE_DEPTH] = {};
    DevSplineTable* devTerrainParams = nullptr;
    // Stage-1 (dev_check-only) survivors, accumulated across the WHOLE request (all
    // batches/sub-batches, both pipeline slots) exactly like devOut/devCount already do --
    // NOT per-slot, since densityFilterKernel runs once at the end of runScan() over
    // everything accumulated here, not once per sub-batch. Same capacity as devOut: this
    // is exactly the volume --no-density-prefilter already proves the buffer handles today
    // (dev_check's superset, before the prefilter narrows it down).
    Candidate* devStage1 = nullptr;
    unsigned int* devStage1Count = nullptr;
    Candidate* devOut = nullptr;
    unsigned int* devCount = nullptr;
    unsigned int capacity = 1u << 22; // 4M candidates
    SwampHutConfig cfg{};
};

static WorkerBuffers allocateBuffers() {
    WorkerBuffers b;
    {
        size_t freeB = 0, totB = 0;
        cudaMemGetInfo(&freeB, &totB);
        size_t perSeed = (sizeof(DevNoiseCache) + sizeof(DevClimateCache) + sizeof(long long) + 3) * (size_t)PIPELINE_DEPTH;
        size_t reserve = (size_t)1 << 30;
        size_t budget = freeB > reserve ? (freeB - reserve) / 2 : 0;
        long long fit = (long long)(budget / perSeed);
        if (fit < 64) fit = 64;
        if (fit < MAX_SEEDS_PER_BATCH) MAX_SEEDS_PER_BATCH = (int)fit;
    }
    for (int i = 0; i < PIPELINE_DEPTH; i++) {
        CUDA_OK(cudaMallocHost(&b.seedStaging[i], sizeof(long long) * (size_t)MAX_SEEDS_PER_BATCH));
        CUDA_OK(cudaMalloc(&b.devCaches[i], sizeof(DevNoiseCache) * (size_t)MAX_SEEDS_PER_BATCH));
        CUDA_OK(cudaMalloc(&b.devSeeds[i], sizeof(long long) * (size_t)MAX_SEEDS_PER_BATCH));
        CUDA_OK(cudaMalloc(&b.devClimateCache[i], sizeof(DevClimateCache) * (size_t)MAX_SEEDS_PER_BATCH));
        CUDA_OK(cudaMalloc(&b.devSurvivorFlags[i], sizeof(unsigned char) * (size_t)MAX_SEEDS_PER_BATCH));
        CUDA_OK(cudaMalloc(&b.devAliveFlags[i], sizeof(unsigned char) * (size_t)MAX_SEEDS_PER_BATCH));
        CUDA_OK(cudaMalloc(&b.devErosionFlags[i], sizeof(unsigned char) * (size_t)MAX_SEEDS_PER_BATCH));
        CUDA_OK(cudaStreamCreate(&b.streams[i]));
        CUDA_OK(cudaEventCreate(&b.evBatchStart[i]));
        CUDA_OK(cudaEventCreate(&b.evUploadDone[i]));
        CUDA_OK(cudaEventCreate(&b.evConstructDone[i]));
        CUDA_OK(cudaEventCreate(&b.evCheckDone[i]));
    }

    // terrainParams is entirely seed-independent (vanilla constants only) -- built ONCE
    // on the host (needs the shared_ptr Spline tree), flattened, and uploaded once. Every
    // densityFilterKernel invocation just copies this shared table in, instead of
    // rebuilding an identical tree per survivor.
    DevSplineTable hostTerrainParams = flattenTerrainParameters(VanillaTerrainParameters::createSurfaceParameters());
    CUDA_OK(cudaMalloc(&b.devTerrainParams, sizeof(DevSplineTable)));
    CUDA_OK(cudaMemcpy(b.devTerrainParams, &hostTerrainParams, sizeof(DevSplineTable), cudaMemcpyHostToDevice));

    CUDA_OK(cudaMalloc(&b.devOut, sizeof(Candidate) * b.capacity));
    CUDA_OK(cudaMalloc(&b.devCount, sizeof(unsigned int)));
    CUDA_OK(cudaMalloc(&b.devStage1, sizeof(Candidate) * b.capacity));
    CUDA_OK(cudaMalloc(&b.devStage1Count, sizeof(unsigned int)));

    b.cfg = swampHutConfigModern();
    return b;
}

static void freeBuffers(WorkerBuffers& b) {
    for (int i = 0; i < PIPELINE_DEPTH; i++) {
        cudaFreeHost(b.seedStaging[i]);
        cudaFree(b.devCaches[i]); cudaFree(b.devSeeds[i]);
        cudaFree(b.devClimateCache[i]); cudaFree(b.devSurvivorFlags[i]); cudaFree(b.devAliveFlags[i]); cudaFree(b.devErosionFlags[i]);
        cudaStreamDestroy(b.streams[i]);
        cudaEventDestroy(b.evBatchStart[i]); cudaEventDestroy(b.evUploadDone[i]);
        cudaEventDestroy(b.evConstructDone[i]); cudaEventDestroy(b.evCheckDone[i]);
    }
    cudaFree(b.devOut); cudaFree(b.devCount); cudaFree(b.devTerrainParams);
    cudaFree(b.devStage1); cudaFree(b.devStage1Count);
}

// Runs one full scan (batch loop, optional self-test, candidate CSV) against ALREADY
// allocated buffers. Everything here is exactly the classic single-shot main()'s body,
// just parameterized so it can run more than once per process for --worker. devCount is
// reset at the top of every call -- it must NOT accumulate across scans.
static int runScan(const ScanParams& p, WorkerBuffers& buf) {
    // --quad-bases: upload the base list once per process (identical for every request,
    // so a worker reuses it across scans -- reload only if a different path is asked for).
    if (p.useQuadBases) {
        if (buf.quadBasesLoadedPath != p.quadBasesPath) {
            if (buf.devQuadBases) { cudaFree(buf.devQuadBases); buf.devQuadBases = nullptr; }
            CUDA_OK(cudaMalloc(&buf.devQuadBases, sizeof(unsigned long long) * p.quadBases.size()));
            CUDA_OK(cudaMemcpy(buf.devQuadBases, p.quadBases.data(),
                               sizeof(unsigned long long) * p.quadBases.size(),
                               cudaMemcpyHostToDevice));
            buf.quadBasesLoadedPath = p.quadBasesPath;
            std::printf("uploaded %zu quad-hut bases to device\n", p.quadBases.size());
        }
    }
    int numSeeds = (p.useSeedGen || p.useQuadBases) ? p.seedGenCount : (int)p.seeds.size();
    int regionsX = p.maxRX - p.minRX;
    int regionsZ = p.maxRZ - p.minRZ;
    long long totalRegions = (long long)regionsX * regionsZ;
    long long totalUnits = totalRegions * (long long)numSeeds;
    bool useDensityPrefilter = !p.noDensityPrefilter;

    // Climate gating pays off only when a large fraction of seeds fail climate on EVERY
    // region in the search area -- then the ~74%-of-cost remaining samplers get skipped for
    // most seeds (measured 1.68x at 9 regions/seed). As the area widens, nearly every seed
    // has SOME climate-passing region somewhere, so the gate skips almost nothing while
    // still paying for its extra full pass over every (region, seed) pair -- measured ~25%
    // SLOWER at 400 regions/seed. CLIMATE_GATE_MAX_REGIONS is the measured crossover; see
    // the sweep in this file's history. Override with --climate-gate / --no-climate-gate
    // (mainly for A/B-ing the two paths against each other).
    // Measured sweep (4M seeds, gate forced on vs off, seconds):
    //   9 regions: 1.088 vs 1.851 (gate wins 1.70x)   25: 1.387 vs 1.884 (wins 1.36x)
    //   64:        2.093 vs 2.056 (essentially tied)  100: 2.911 vs 2.351 (loses 1.24x)
    //   196:       4.471 vs 3.079 (loses 1.45x)       400: 7.491 vs 4.763 (loses 1.57x)
    // Crossover sits between 25 and 64; 32 keeps us safely on the winning side of it.
    const long long CLIMATE_GATE_MAX_REGIONS = 32;
    bool useClimateGate = (p.climateGateMode == 1) ||
                          (p.climateGateMode == 0 && totalRegions <= CLIMATE_GATE_MAX_REGIONS);

    CUDA_OK(cudaMemset(buf.devCount, 0, sizeof(unsigned int)));
    CUDA_OK(cudaMemset(buf.devStage1Count, 0, sizeof(unsigned int)));

    if (p.useSeedGen) {
        std::printf("seeds=%d (generated on-device, base=%llu) regions x:[%d,%d) z:[%d,%d)  perSeed=%lld  totalChecks=%lld  maxHeight=%d  densityPrefilter=%s\n",
                    numSeeds, p.seedGenBase, p.minRX, p.maxRX, p.minRZ, p.maxRZ, totalRegions, totalUnits, p.maxHeight,
                    useDensityPrefilter ? "on" : "off");
    } else if (numSeeds == 1) {
        std::printf("seed=%lld regions x:[%d,%d) z:[%d,%d)  total=%lld  maxHeight=%d  densityPrefilter=%s\n",
                    p.seeds[0], p.minRX, p.maxRX, p.minRZ, p.maxRZ, totalRegions, p.maxHeight,
                    useDensityPrefilter ? "on" : "off");
    } else {
        std::printf("seeds=%d (batched) regions x:[%d,%d) z:[%d,%d)  perSeed=%lld  totalChecks=%lld  maxHeight=%d  densityPrefilter=%s\n",
                    numSeeds, p.minRX, p.maxRX, p.minRZ, p.maxRZ, totalRegions, totalUnits, p.maxHeight,
                    useDensityPrefilter ? "on" : "off");
    }

    int numBatches = (numSeeds + MAX_SEEDS_PER_BATCH - 1) / MAX_SEEDS_PER_BATCH;

    double totalBuildSecs = 0, totalUploadSecs = 0, totalKernelSecs = 0;
    long long grandDoneUnits = 0;
    long long selfTestChecked = 0, selfTestMismatches = 0;
    // Accumulates --seed-gen's on-device-generated seeds across the WHOLE request, in
    // batch order, only when self-test needs them (see the accumulation point below). For
    // --seed-list/--seed mode, p.seeds already IS this complete list -- nothing to build.
    std::vector<long long> selfTestSeeds;
    auto tOverall0 = std::chrono::steady_clock::now();
    auto lastReport = tOverall0;

    for (int batchIdx = 0; batchIdx < numBatches; batchIdx++) {
        int slot = batchIdx % PIPELINE_DEPTH;
        cudaStream_t stream = buf.streams[slot];

        // This slot's host+device buffers are about to be overwritten -- make sure
        // whatever batch last used them (issued PIPELINE_DEPTH iterations ago) has
        // actually finished with them first, then bank its timing now that it's safe to
        // read the events. Does NOT touch the OTHER slot's stream, which keeps running
        // concurrently on the GPU the whole time this call blocks.
        if (batchIdx >= PIPELINE_DEPTH) {
            CUDA_OK(cudaStreamSynchronize(stream));
            float upMs = 0, buildMs = 0, checkMs = 0;
            CUDA_OK(cudaEventElapsedTime(&upMs, buf.evBatchStart[slot], buf.evUploadDone[slot]));
            CUDA_OK(cudaEventElapsedTime(&buildMs, buf.evUploadDone[slot], buf.evConstructDone[slot]));
            CUDA_OK(cudaEventElapsedTime(&checkMs, buf.evConstructDone[slot], buf.evCheckDone[slot]));
            totalUploadSecs += upMs / 1000.0;
            totalBuildSecs += buildMs / 1000.0;
            totalKernelSecs += checkMs / 1000.0;
        }

        int batchStart = batchIdx * MAX_SEEDS_PER_BATCH;
        int batchSeeds = std::min(MAX_SEEDS_PER_BATCH, numSeeds - batchStart);
        if (numBatches > 1) {
            std::printf("--- batch %d/%d: %d seed(s) ---\n", batchIdx + 1, numBatches, batchSeeds);
        }

        // Upload just the raw seed VALUES (tiny -- 8 bytes/seed), then build every seed's
        // noise cache directly on the GPU. See constructCacheKernel's comment and
        // validate_device_construct.exe (19,640,500/19,640,500 bit-exact vs the host path
        // this replaces). Everything below is enqueued on this slot's own stream, so it
        // overlaps with whatever the OTHER slot's stream is still finishing (per-batch
        // seed-upload/GPU-construct/kernel timing is therefore no longer available
        // synchronously per batch -- see the "totals across N batch(es)" summary instead,
        // which still adds up correctly via the per-slot events banked above/below).
        //
        // useSeedGen: there's nothing to stage or upload -- constructClimateKernel (the
        // first kernel to touch each seed now) derives and writes each seed itself -- so
        // this skips straight to the construct launch. evUploadDone is still recorded
        // immediately (0 elapsed) purely so the existing event-timing bookkeeping below
        // doesn't need a special case.
        CUDA_OK(cudaEventRecord(buf.evBatchStart[slot], stream));
        // --quad-bases derives its seeds on-device exactly like --seed-gen does, so it has
        // nothing to stage either -- and p.seeds is EMPTY in that mode, so falling into the
        // staging branch would index a zero-length vector.
        if (p.useSeedGen || p.useQuadBases) {
            CUDA_OK(cudaEventRecord(buf.evUploadDone[slot], stream));
        } else {
            for (int i = 0; i < batchSeeds; i++) buf.seedStaging[slot][i] = p.seeds[batchStart + i];
            CUDA_OK(cudaMemcpyAsync(buf.devSeeds[slot], buf.seedStaging[slot], sizeof(long long) * (size_t)batchSeeds,
                                    cudaMemcpyHostToDevice, stream));
            CUDA_OK(cudaEventRecord(buf.evUploadDone[slot], stream));
        }

        int constructBlock = 256;
        int constructBlocks = (batchSeeds + constructBlock - 1) / constructBlock;
        unsigned long long batchSeedGenBase = p.seedGenBase + (unsigned long long)batchStart;

        // Slice by Z rows so no single kernel launch runs long enough to trip the Windows
        // display-driver watchdog (TDR), and so progress is visible on big scans. Shared by
        // BOTH the climate-check and full-check loops below -- climateCheckKernel does
        // strictly less work per thread than checkKernel, so reusing checkKernel's own
        // sizing keeps every climate-check slice safely under budget too.
        long long targetPerSlice = 200LL * 1000 * 1000; // ~0.6s per launch at measured rates
        int rowsPerSlice = (int)(targetPerSlice / ((long long)regionsX * batchSeeds));
        if (rowsPerSlice < 1) rowsPerSlice = 1;
        if (rowsPerSlice > regionsZ) rowsPerSlice = regionsZ;

        // STAGE -1: position gate (optional). Runs before EVERY other kernel, including the
        // otherwise-unconditional climate build, because that build is exactly the cost this
        // is here to avoid paying for seeds whose huts land nowhere near the target. Gate
        // off (maxHutDist < 0) => all seeds alive => pipeline behaves exactly as before.
        const bool usePositionGate = (p.maxHutDist >= 0);
        if (usePositionGate) {
            CUDA_OK(cudaMemsetAsync(buf.devAliveFlags[slot], 0, sizeof(unsigned char) * (size_t)batchSeeds, stream));
            long long gateBlocks = ((long long)regionsX * regionsZ + p.block - 1) / p.block;
            dim3 gateGrid((unsigned int)gateBlocks, (unsigned int)batchSeeds);
            positionGateKernel<<<gateGrid, p.block, 0, stream>>>(
                buf.devSeeds[slot], batchSeeds, p.useSeedGen, batchSeedGenBase,
                buf.devQuadBases, p.useQuadBases,
                p.minRX, p.minRZ, regionsX, regionsZ,
                buf.cfg.salt, buf.cfg.spacing, buf.cfg.offset(),
                p.gateCenterX, p.gateCenterZ, p.maxHutDist * p.maxHutDist,
                buf.devAliveFlags[slot]);
            CUDA_OK(cudaGetLastError());
        } else {
            CUDA_OK(cudaMemsetAsync(buf.devAliveFlags[slot], 1, sizeof(unsigned char) * (size_t)batchSeeds, stream));
        }

        if (useClimateGate) {
            // Reset this slot's survivor flags -- reused/overwritten every PIPELINE_DEPTH
            // batches, must not carry stale flags over from whatever batch last used it.
            CUDA_OK(cudaMemsetAsync(buf.devSurvivorFlags[slot], 0, sizeof(unsigned char) * (size_t)batchSeeds, stream));

            if (p.requireAllRegions) {
                // Staged build: erosion -> reject -> temperature+ridge. Only worth it under
                // all-regions semantics, where the erosion gate is highly selective (every
                // region must pass, so one bad region kills the seed).
                CUDA_OK(cudaMemsetAsync(buf.devErosionFlags[slot], 0, sizeof(unsigned char) * (size_t)batchSeeds, stream));
                constructErosionKernel<<<constructBlocks, constructBlock, 0, stream>>>(
                    buf.devSeeds[slot], batchSeeds, p.useSeedGen, batchSeedGenBase,
                    buf.devQuadBases, p.useQuadBases,
                    buf.devAliveFlags[slot], buf.devClimateCache[slot]);
                CUDA_OK(cudaGetLastError());

                int allBlocks = (batchSeeds + p.block - 1) / p.block;
                erosionCheckAllKernel<<<allBlocks, p.block, 0, stream>>>(
                    buf.devClimateCache[slot], buf.devSeeds[slot], batchSeeds,
                    p.minRX, p.minRZ, regionsX, regionsZ,
                    buf.cfg.salt, buf.cfg.spacing, buf.cfg.offset(),
                    buf.devAliveFlags[slot], buf.devErosionFlags[slot]);
                CUDA_OK(cudaGetLastError());

                constructTempRidgeKernel<<<constructBlocks, constructBlock, 0, stream>>>(
                    buf.devSeeds[slot], batchSeeds, buf.devErosionFlags[slot], buf.devClimateCache[slot]);
                CUDA_OK(cudaGetLastError());
            } else {
                // Climate trio (erosion/temperature/ridge, ~26% of a seed's total sampler
                // cost) built for EVERY seed that survived the position gate -- see
                // constructClimateKernel's comment.
                constructClimateKernel<<<constructBlocks, constructBlock, 0, stream>>>(
                    buf.devSeeds[slot], batchSeeds, p.useSeedGen, batchSeedGenBase,
                    buf.devQuadBases, p.useQuadBases,
                    buf.devAliveFlags[slot], buf.devClimateCache[slot]);
                CUDA_OK(cudaGetLastError());
            }

            // Climate-only pass over every (region, seed) -- marks which seeds have at
            // least one climate-passing region anywhere in the search area
            // (survivorFlags), so the remaining ~74%-of-cost samplers only get built for
            // those. See climateCheckKernel's comment.
            if (p.requireAllRegions) {
                // One thread per seed, all regions inspected with early-exit -- see
                // climateCheckAllKernel. Not sliced by region rows: the kernel walks the
                // whole box itself so it can bail on the first failing region.
                int allBlocks = (batchSeeds + p.block - 1) / p.block;
                // Gated on the EROSION flags, not the position flags: temperature/ridge were
                // only built for erosion survivors, so any other seed's cache holds stale
                // data from an earlier batch in this slot.
                climateCheckAllKernel<<<allBlocks, p.block, 0, stream>>>(
                    buf.devClimateCache[slot], buf.devSeeds[slot], batchSeeds,
                    p.minRX, p.minRZ, regionsX, regionsZ, p.is26_2,
                    buf.cfg.salt, buf.cfg.spacing, buf.cfg.offset(),
                    buf.devErosionFlags[slot], buf.devSurvivorFlags[slot]);
                CUDA_OK(cudaGetLastError());
            } else
            for (int zStart = 0; zStart < regionsZ; zStart += rowsPerSlice) {
                int rows = rowsPerSlice;
                if (zStart + rows > regionsZ) rows = regionsZ - zStart;
                long long sliceRegions = (long long)regionsX * rows;
                long long blocksCount = (sliceRegions + p.block - 1) / p.block;
                dim3 grid((unsigned int)blocksCount, (unsigned int)batchSeeds);
                climateCheckKernel<<<grid, p.block, 0, stream>>>(buf.devClimateCache[slot], buf.devSeeds[slot],
                                              p.minRX, p.minRZ + zStart, regionsX, rows, p.is26_2,
                                              buf.cfg.salt, buf.cfg.spacing, buf.cfg.offset(),
                                              p.gateCenterX, p.gateCenterZ,
                                              usePositionGate ? p.maxHutDist * p.maxHutDist : -1LL,
                                              buf.devAliveFlags[slot], buf.devSurvivorFlags[slot]);
                CUDA_OK(cudaGetLastError());
            }

            // Remaining 11 samplers -- only for climate survivors (see
            // constructCacheKernel's comment).
            constructCacheKernel<<<constructBlocks, constructBlock, 0, stream>>>(
                buf.devSeeds[slot], batchSeeds, buf.devSurvivorFlags[slot], buf.devClimateCache[slot], buf.devCaches[slot]);
            CUDA_OK(cudaGetLastError());
        } else {
            // Gate disabled: mark every seed a "survivor" (checkKernel gates on this
            // unconditionally) and build all 14 samplers in one pass, exactly as the
            // pre-climate-gating code did.
            // survivorFlags starts as a copy of aliveFlags rather than all-1: checkKernel
            // gates on survivorFlags alone here (no climate pass runs to narrow it), so a
            // position-gated-out seed must already read as a non-survivor or checkKernel
            // would sample its never-built, stale cache.
            CUDA_OK(cudaMemcpyAsync(buf.devSurvivorFlags[slot], buf.devAliveFlags[slot],
                                    sizeof(unsigned char) * (size_t)batchSeeds,
                                    cudaMemcpyDeviceToDevice, stream));
            constructFullCacheKernel<<<constructBlocks, constructBlock, 0, stream>>>(
                buf.devSeeds[slot], batchSeeds, p.useSeedGen, batchSeedGenBase,
                buf.devQuadBases, p.useQuadBases,
                buf.devAliveFlags[slot], buf.devCaches[slot]);
            CUDA_OK(cudaGetLastError());
        }
        CUDA_OK(cudaEventRecord(buf.evConstructDone[slot], stream));

        // --seed-gen + --self-test: the final self-test comparison pass (after the whole
        // request, once density-filtering has run) needs the complete list of seeds this
        // request actually used -- --seed-list already has that in p.seeds, but --seed-gen
        // seeds only ever exist on the device. Force a sync here (self-test already accepts
        // losing pipeline overlap for correctness verification) and read this batch's
        // generated seeds back before they're overwritten by a later batch reusing this
        // slot. Placed after the if/else above so it covers BOTH paths -- whichever
        // construct kernel ran was the first to touch each seed, so seeds[] is guaranteed
        // populated by the time this sync returns.
        if (p.selfTest && p.useSeedGen) {
            CUDA_OK(cudaStreamSynchronize(stream));
            size_t before = selfTestSeeds.size();
            selfTestSeeds.resize(before + (size_t)batchSeeds);
            CUDA_OK(cudaMemcpy(selfTestSeeds.data() + before, buf.devSeeds[slot],
                               sizeof(long long) * (size_t)batchSeeds, cudaMemcpyDeviceToHost));
        }

        for (int zStart = 0; zStart < regionsZ; zStart += rowsPerSlice) {
            int rows = rowsPerSlice;
            if (zStart + rows > regionsZ) rows = regionsZ - zStart;
            long long sliceRegions = (long long)regionsX * rows;
            long long blocksCount = (sliceRegions + p.block - 1) / p.block;
            dim3 grid((unsigned int)blocksCount, (unsigned int)batchSeeds);
            checkKernel<<<grid, p.block, 0, stream>>>(buf.devCaches[slot], buf.devSeeds[slot], buf.devSurvivorFlags[slot],
                                          p.minRX, p.minRZ + zStart, regionsX, rows, p.maxHeight, p.is26_2,
                                          buf.cfg.salt, buf.cfg.spacing, buf.cfg.offset(),
                                          p.gateCenterX, p.gateCenterZ,
                                          usePositionGate ? p.maxHutDist * p.maxHutDist : -1LL,
                                          buf.devStage1, buf.devStage1Count, buf.capacity);
            CUDA_OK(cudaGetLastError());
            // Not synchronized here (that's the whole point of the pipeline) -- so
            // grandDoneUnits below counts launched, not-necessarily-completed work, making
            // the periodic "% done" progress print an estimate. Only matters for a single
            // scan big enough to run past the 5s report threshold.
            grandDoneUnits += sliceRegions * batchSeeds;

            auto now = std::chrono::steady_clock::now();
            if (std::chrono::duration<double>(now - lastReport).count() >= 5.0 && grandDoneUnits < totalUnits) {
                double el = std::chrono::duration<double>(now - tOverall0).count();
                std::printf("  %.1f%%  %lld/%lld region-seed checks  %.1f M/s  elapsed %.0fs\n",
                            100.0 * grandDoneUnits / totalUnits, grandDoneUnits, totalUnits,
                            grandDoneUnits / el / 1e6, el);
                std::fflush(stdout);
                lastReport = now;
            }
        }
        CUDA_OK(cudaEventRecord(buf.evCheckDone[slot], stream));
    }

    // Drain whatever's still in flight -- the last up-to-PIPELINE_DEPTH batches never hit
    // the reuse-triggered sync inside the loop above -- and bank their timing too.
    {
        int slotsUsed = std::min(numBatches, PIPELINE_DEPTH);
        for (int s = 0; s < slotsUsed; s++) {
            CUDA_OK(cudaStreamSynchronize(buf.streams[s]));
            float upMs = 0, buildMs = 0, checkMs = 0;
            CUDA_OK(cudaEventElapsedTime(&upMs, buf.evBatchStart[s], buf.evUploadDone[s]));
            CUDA_OK(cudaEventElapsedTime(&buildMs, buf.evUploadDone[s], buf.evConstructDone[s]));
            CUDA_OK(cudaEventElapsedTime(&checkMs, buf.evConstructDone[s], buf.evCheckDone[s]));
            totalUploadSecs += upMs / 1000.0;
            totalBuildSecs += buildMs / 1000.0;
            totalKernelSecs += checkMs / 1000.0;
        }
    }

    // STAGE 2: density-filter whatever dev_check() survivors accumulated in devStage1
    // across the WHOLE request (all batches/sub-batches) -- see densityFilterKernel's
    // comment for why this runs once here instead of once per sub-batch. Skipped for
    // --no-density-prefilter, where stage 1's own output already IS the final result
    // (matches checkKernel/phase1Kernel's old useDensityPrefilter=false behavior exactly).
    unsigned int stage1Count = 0;
    CUDA_OK(cudaMemcpy(&stage1Count, buf.devStage1Count, sizeof(unsigned int), cudaMemcpyDeviceToHost));
    if (stage1Count > buf.capacity) {
        std::printf("WARNING: %u dev_check() survivor(s) exceeded stage-1 capacity %u; only the first %u were density-filtered\n",
                    stage1Count, buf.capacity, buf.capacity);
        stage1Count = buf.capacity;
    }

    unsigned int found = 0;
    std::vector<Candidate> results;
    if (useDensityPrefilter) {
        if (stage1Count > 0) {
            // Scratch space sized EXACTLY to the real survivor count, known now (not the
            // full 4M-candidate buf.capacity -- see densityFilterKernel's comment for why
            // this can't be a local/stack variable). Allocated fresh each time rather than
            // held permanently: this only runs once per --worker request (not per
            // sub-batch), and survivors are rare enough that the alloc/free pair is
            // negligible next to what deferring density-context construction saves.
            DevNoiseCache* scratchCaches = nullptr;
            DevDensityContext* scratchDensities = nullptr;
            CUDA_OK(cudaMalloc(&scratchCaches, sizeof(DevNoiseCache) * (size_t)stage1Count));
            CUDA_OK(cudaMalloc(&scratchDensities, sizeof(DevDensityContext) * (size_t)stage1Count));

            int densityBlock = 256;
            unsigned int densityBlocks = (stage1Count + (unsigned int)densityBlock - 1) / (unsigned int)densityBlock;
            densityFilterKernel<<<densityBlocks, densityBlock>>>(
                buf.devStage1, buf.devStage1Count, p.maxHeight, buf.devTerrainParams,
                scratchCaches, scratchDensities,
                buf.devOut, buf.devCount, buf.capacity);
            CUDA_OK(cudaGetLastError());
            // Sync before freeing -- the kernel launch above is async, and freeing scratch
            // memory it's still using would be a use-after-free.
            CUDA_OK(cudaDeviceSynchronize());
            CUDA_OK(cudaFree(scratchCaches));
            CUDA_OK(cudaFree(scratchDensities));
        }
        CUDA_OK(cudaMemcpy(&found, buf.devCount, sizeof(unsigned int), cudaMemcpyDeviceToHost));
        unsigned int stored = found > buf.capacity ? buf.capacity : found;
        results.resize(stored);
        if (stored) {
            CUDA_OK(cudaMemcpy(results.data(), buf.devOut, sizeof(Candidate) * stored, cudaMemcpyDeviceToHost));
        }
    } else {
        found = stage1Count;
        results.resize(stage1Count);
        if (stage1Count) {
            CUDA_OK(cudaMemcpy(results.data(), buf.devStage1, sizeof(Candidate) * stage1Count, cudaMemcpyDeviceToHost));
        }
    }

    auto tOverallEnd = std::chrono::steady_clock::now();
    double overallSecs = std::chrono::duration<double>(tOverallEnd - tOverall0).count();

    // NOT printing the per-slot upload/construct/kernel breakdown here (used to as
    // "totals across N batch(es): ..."): each number is genuine wall-clock time on its OWN
    // stream, but the two pipeline slots run concurrently and contend for the same SMs, so
    // summing them across sub-batches double-counts real overlap and can legitimately
    // exceed the actual wall time below (confirmed directly: a case measured 6.5s of
    // reported "GPU-construct" inside a 1.9s real iteration). This got dramatically more
    // visible once --seed-gen removed the host-side per-batch prep that used to throttle
    // how far the launches could race ahead. overallSecs below is the trustworthy number --
    // it's real wall-clock time, not a sum of per-stream event deltas.
    // Per-stage split: these were being accumulated and then discarded, which hid where
    // the time actually goes. "construct" is the sampler builds (climate trio for every
    // seed + the remaining 11 for survivors); "check" is climate/dev_check over regions.
    // Knowing the ratio is what tells you whether more filtering helps at all -- filtering
    // only ever reduces the check side and the survivor part of construct, never the
    // unconditional climate build.
    {
        double acct = totalUploadSecs + totalBuildSecs + totalKernelSecs;
        if (acct > 0) {
            std::printf("  stages: upload %.2fs (%.0f%%)  construct %.2fs (%.0f%%)  check %.2fs (%.0f%%)\n",
                        totalUploadSecs, 100.0 * totalUploadSecs / acct,
                        totalBuildSecs,  100.0 * totalBuildSecs  / acct,
                        totalKernelSecs, 100.0 * totalKernelSecs / acct);
        }
    }
    std::printf("kernel: %.3f s  candidates=%u  (%.2f M region-seed-checks/s across %d seed(s))\n",
                overallSecs, found, totalUnits / overallSecs / 1e6, numSeeds);
    if (found > buf.capacity) {
        std::printf("WARNING: %u candidates exceeded capacity %u; only the first %u were kept\n",
                    found, buf.capacity, buf.capacity);
    }

    // Self-test comparison runs ONCE here, after the whole request (all batches AND stage
    // 2) has completed, instead of per-batch like before -- devOut/results above isn't
    // populated until density-filtering has run over EVERYTHING accumulated across the
    // whole request, not just one batch's worth.
    if (p.selfTest) {
        const std::vector<long long>& allSeeds = p.useSeedGen ? selfTestSeeds : p.seeds;
        long long step = totalRegions > 20000 ? totalRegions / 20000 : 1;
        for (long long thisSeed : allSeeds) {
            WorldNoiseCache hc = WorldNoiseCache::build(thisSeed);
            DensityContext hd = DensityContext::build(hc, thisSeed);

            std::vector<long long> hits;
            for (auto& c : results) {
                if (c.seed == thisSeed) hits.push_back(((long long)c.chunkX << 32) ^ (unsigned int)c.chunkZ);
            }
            std::sort(hits.begin(), hits.end());

            long long mismatches = 0;
            for (long long idx = 0; idx < totalRegions; idx += step) {
                int rx = p.minRX + (int)(idx % regionsX);
                int rz = p.minRZ + (int)(idx / regionsX);
                int outRx, outRz, outChunkX, outChunkZ;
                bool devPass, hostPass;
                bool ok = selfTestOneRegion(hc, hd, thisSeed, rx, rz, p.maxHeight, p.is26_2,
                                            useDensityPrefilter, buf.cfg, hits,
                                            &outRx, &outRz, &outChunkX, &outChunkZ, &devPass, &hostPass);
                if (!ok) {
                    if (mismatches < 5) {
                        std::printf("  MISMATCH seed=%lld region(%d,%d) chunk(%d,%d): device=%d host=%d\n",
                                    thisSeed, outRx, outRz, outChunkX, outChunkZ, (int)devPass, (int)hostPass);
                    }
                    mismatches++;
                }
                selfTestChecked++;
            }
            selfTestMismatches += mismatches;
        }
        std::printf("self-test: checked %lld sampled regions across %d seed(s), %lld mismatches -> %s\n",
                    selfTestChecked, numSeeds, selfTestMismatches, selfTestMismatches == 0 ? "PASS" : "FAIL");
        if (selfTestMismatches) { std::fprintf(stderr, "self-test FAILED\n"); return 1; }
    }

    if (!p.outPath.empty()) {
        FILE* f = std::fopen(p.outPath.c_str(), "w");
        if (!f) { std::fprintf(stderr, "cannot write %s\n", p.outPath.c_str()); return 1; }
        // gpuEst is a trailing 6th column on purpose: DebugPhase2Batch splits on ',' and
        // reads only fields 0..2, so existing consumers ignore it.
        std::fprintf(f, "seed,chunkX,chunkZ,blockX,blockZ,gpuEst\n");
        for (auto& c : results) {
            std::fprintf(f, "%lld,%d,%d,%d,%d,%.3f\n", c.seed, c.chunkX, c.chunkZ, c.chunkX * 16, c.chunkZ * 16, c.est);
        }
        std::fclose(f);
        std::printf("wrote %zu candidates to %s\n", results.size(), p.outPath.c_str());
    }

    return 0;
}

// Splits a stdin line into whitespace-separated tokens, the same shape argv would be in.
// Strips a leading UTF-8 BOM if present (defensive -- Java's own String/OutputStream
// writes never prepend one, unlike .NET's, but stripping is free and protects against
// any future caller that does).
static std::vector<std::string> tokenizeLine(std::string line) {
    if (line.size() >= 3 && (unsigned char)line[0] == 0xEF &&
        (unsigned char)line[1] == 0xBB && (unsigned char)line[2] == 0xBF) {
        line.erase(0, 3);
    }
    std::vector<std::string> out;
    std::istringstream iss(line);
    std::string tok;
    while (iss >> tok) out.push_back(tok);
    return out;
}

// --worker: one CUDA context, one set of allocated buffers, read scan requests from stdin
// forever. Each line means exactly what the same flags mean on the normal command line --
// same parser, same runScan(). A bad line reports to stderr/===SCAN_ERROR=== and does NOT
// kill the worker, since one malformed request must not take a long-running caller (e.g.
// GpuHutSearch.java's --continuous loop) down with it.
static int runWorker() {
    WorkerBuffers buf = allocateBuffers();
    std::printf("===WORKER_READY===\n");
    std::fflush(stdout);

    std::string line;
    while (std::getline(std::cin, line)) {
        if (line.empty()) continue;
        std::vector<std::string> tokens = tokenizeLine(line);
        ScanParams p;
        if (!parseScanArgs(tokens, p, true)) {
            std::printf("===SCAN_ERROR: bad request line===\n");
            std::fflush(stdout);
            continue;
        }
        int rc = runScan(p, buf);
        std::printf("===SCAN_DONE rc=%d===\n", rc);
        std::fflush(stdout);
    }

    freeBuffers(buf);
    return 0;
}

// Reads whitespace/comma-separated int64 seeds from a file, appending to `out`.
// Bulk-reads the whole file into one buffer and scans over memory instead of the previous
// fgetc()-per-character approach -- measured 5x faster on a real 4,000,000-seed / 81.5MB
// file (1074ms -> 209ms), and this cost wasn't even visible in any of runScan()'s own
// timing (it happens before runScan() is called, while parsing the request), so it was
// pure hidden overhead on every single --worker iteration. Verified byte-for-byte
// identical output against the old parser (0/4,000,000 mismatches) before replacing it.
static bool readSeedList(const char* path, std::vector<long long>& out) {
    FILE* f = std::fopen(path, "rb");
    if (!f) return false;
    std::fseek(f, 0, SEEK_END);
    long sz = std::ftell(f);
    std::fseek(f, 0, SEEK_SET);
    std::vector<char> buf((size_t)sz + 1);
    size_t rd = std::fread(buf.data(), 1, (size_t)sz, f);
    std::fclose(f);
    buf[rd] = '\0';

    const char* p = buf.data();
    const char* end = p + rd;
    while (p < end) {
        while (p < end && !(std::isdigit((unsigned char)*p) || *p == '-')) p++;
        if (p >= end) break;
        char* next;
        // strtoull, not strtoll: seeds above LLONG_MAX written in unsigned
        // decimal form (no leading '-') get silently clamped to LLONG_MAX by
        // strtoll instead of parsed correctly -- confirmed this dropped a
        // real seed (a genuine y=-48 hut) when reading a generator that
        // writes unsigned decimal. strtoull correctly parses both an
        // unsigned decimal AND a leading '-' (the C standard has it negate
        // the unsigned result for a leading minus, which is exactly the
        // right 64-bit reinterpretation for a signed seed written that way).
        unsigned long long uv = std::strtoull(p, &next, 10);
        if (next == p) { p++; continue; }
        out.push_back((long long)uv);
        p = next;
    }
    return true;
}

// Runs the existing validated host check()+densityPrefilter for one region and compares
// against whether the device found that exact (chunkX,chunkZ) among `hits` (sorted).
// Shared between the self-test blocks so the two copies can't drift apart.
static bool selfTestOneRegion(const WorldNoiseCache& hc, const DensityContext& hd,
                              long long seed, int rx, int rz, int maxHeight, bool is26_2,
                              bool useDensityPrefilter, const SwampHutConfig& cfg,
                              const std::vector<long long>& hits,
                              int* outRx, int* outRz, int* outChunkX, int* outChunkZ,
                              bool* outDevPass, bool* outHostPass) {
    ChunkPos p = swampHutGetInRegion(seed, rx, rz, cfg);
    int hutX = 16 * p.x, hutZ = 16 * p.z;
    bool hostPass = true;
    {
        int climateX = hutX + 8, climateZ = hutZ + 8;
        int heightX = hutX + 3, heightZ = hutZ + 3;
        double er = hc.erosion.sample(climateX / 4.0, 0, climateZ / 4.0);
        if (er < 0.55) hostPass = false;
        if (hostPass) {
            double tp = hc.temperature.sample(climateX / 4.0, 0, climateZ / 4.0);
            if (tp > 0.2 || tp < -0.45) hostPass = false;
        }
        if (hostPass) {
            double rg = hc.ridge.sample(climateX / 4.0, 0, climateZ / 4.0);
            if ((rg > 0.42 && rg < 0.91) || (rg < -0.42 && rg > -0.91)) hostPass = false;
            if (hostPass && is26_2 && rg <= -0.91) hostPass = false;
        }
        if (hostPass && Entrance(hc, heightX, 50, heightZ) >= 0) hostPass = false;
        if (hostPass && Entrance(hc, heightX, 60, heightZ) >= 0) hostPass = false;
        if (hostPass && Entrance2(hc, heightX, maxHeight, heightZ) >= 0
                     && Cheese(hc, heightX, maxHeight, heightZ) >= 0) hostPass = false;
        if (hostPass) {
            for (int y = 0; y >= -40 && hostPass; y -= 10) {
                if (maxHeight < y && Entrance2(hc, heightX, y, heightZ) >= 0
                                  && Cheese(hc, heightX, y, heightZ) >= 0) hostPass = false;
            }
        }
        if (hostPass) {
            for (int y = 10; y <= 40 && hostPass; y += 10) {
                if (Entrance(hc, heightX, y, heightZ) >= 0
                    && Cheese(hc, heightX, y, heightZ) >= 0) hostPass = false;
            }
        }
        if (hostPass && hc.continentalness.sample(climateX / 4.0, 0, climateZ / 4.0) < -0.11)
            hostPass = false;
        if (hostPass) {
            for (int y = maxHeight; y <= 60 && hostPass; y += 10) {
                if (hc.aquiferFloodedness.sample(heightX, y * 0.67, heightZ) > 0.41)
                    hostPass = false;
            }
        }
        // Ground truth must match what the kernel actually emits: check() AND (if
        // enabled) the density prefilter, both re-run here on the host.
        if (hostPass && useDensityPrefilter) {
            hostPass = hd.passesDensityPrefilter(seed, hutX, hutZ, maxHeight);
        }
    }
    long long key = ((long long)p.x << 32) ^ (unsigned int)p.z;
    bool devPass = std::binary_search(hits.begin(), hits.end(), key);
    *outRx = rx; *outRz = rz; *outChunkX = p.x; *outChunkZ = p.z;
    *outDevPass = devPass; *outHostPass = hostPass;
    return devPass == hostPass;
}

int main(int argc, char** argv) {
    // Busy-spin instead of the default block/yield heuristic while waiting on
    // cudaStreamSynchronize/cudaDeviceSynchronize -- the pipelined batch loop does ~62 sync
    // calls/iteration (one per sub-batch reuse at PIPELINE_DEPTH=2, for a 4,000,000-seed /
    // 62-sub-batch request), and each one pays some OS wake-up latency under the default
    // policy. Measured ~1.6% faster average iteration time across two independent 8-sample
    // A/B rounds (4.390s -> 4.318s), consistent in direction both times. Must be the very
    // first CUDA call, before any context is created -- trades one CPU core spinning at
    // 100% while waiting for lower sync latency, an easy trade for a batch search tool.
    CUDA_OK(cudaSetDeviceFlags(cudaDeviceScheduleSpin));

    // Raise the per-thread stack size limit -- densityFilterKernel hit "illegal memory
    // access" crashes under the CUDA default. Root cause: dev_splineApply (terrain_shaper.h)
    // recurses into child spline nodes, and its call depth is DATA-DEPENDENT (walks
    // whatever the real spline tree's shape is) -- ptxas's static "cumulative stack size"
    // estimate for a kernel can't fully account for that, so it undercounts. This was
    // already true when dev_passesDensityPrefilter ran inside the old combined
    // check-and-filter kernel, but that kernel's OTHER local usage was small enough to
    // leave slack for the gap; densityFilterKernel ALSO builds a full noise cache +
    // density context first (~33KB of local usage on its own, matching
    // constructCacheKernel's), leaving much less headroom before the same recursion
    // overflows. Must be the very first CUDA calls, before any context is created.
    CUDA_OK(cudaDeviceSetLimit(cudaLimitStackSize, 65536));

    // Line-buffer stdout so progress is visible when piped/redirected on long scans.
    // NOTE: size must be nonzero -- passing 0 here is undefined behavior in MSVC's CRT
    // and was corrupting stdout's buffer state badly enough to trip the /GS stack-cookie
    // check on the very next printf (manifested as STATUS_STACK_BUFFER_OVERRUN with zero
    // output, since it crashed before the first flush could happen).
    std::setvbuf(stdout, nullptr, _IOLBF, 1024);

    std::vector<std::string> tokens(argv + 1, argv + argc);
    bool isWorker = false;
    for (auto& t : tokens) if (t == "--worker") { isWorker = true; break; }

    if (isWorker) {
        return runWorker();
    }

    if (tokens.empty()) { usage(); return 2; }
    for (auto& t : tokens) {
        if (t == "--help" || t == "-h") { usage(); return 0; }
    }

    ScanParams p;
    if (!parseScanArgs(tokens, p, false)) { usage(); return 2; }

    WorkerBuffers buf = allocateBuffers();
    int rc = runScan(p, buf);
    freeBuffers(buf);
    return rc;
}

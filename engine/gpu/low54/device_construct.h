// Device-native construction of the per-seed noise cache -- builds DevNoiseCache and
// DevDensityContext DIRECTLY (fixed-size arrays, precomputed MD5 digests), with no
// std::vector/std::string/MD5-at-runtime anywhere, so this can run on a GPU thread
// exactly as it runs on a host thread.
//
// This exists because the ORIGINAL construction path (WorldNoiseCache::build /
// DensityContext::build in noise_setup.h / density_prefilter.h) needs std::vector for
// amplitudes and std::string for name-derivation, both host-only -- so construction was
// 100% CPU work even though sampling already ran on the GPU. Per-seed cache construction
// (MD5 + Xoroshiro128++ derivation + Perlin permutation-table shuffling) turned out to be
// the actual bottleneck for many-random-seeds searches (see phase1_scan.cu's top-of-file
// history), not GPU compute -- moving construction here lets it run on the GPU too,
// massively parallel across all seeds in a batch instead of 16 CPU threads.
//
// Every function below mirrors its host counterpart EXACTLY (same derivation order, same
// mutation semantics) -- see noise_setup.h / density_prefilter.h / perlin_noise.h for the
// bytecode-verified reference each of these is built to match bit-for-bit. Validated via
// validate_device_construct.cpp before being trusted in the real kernel.
#pragma once
#include "device_noise.h"
#include "device_density.h"
#include "xoroshiro128pp.h"
#include "noise_digests.h"

#ifdef __CUDACC__
#define DC_HD __host__ __device__
#else
#define DC_HD
#endif

// Standard splitmix64 (the same step function used inside java.util.SplittableRandom) --
// a bijection over the full 64-bit space, so distinct counters ALWAYS give distinct
// outputs (no birthday-paradox duplicate seeds like independent random sampling can hit
// over a long-running search) with good avalanche/distribution properties. Used to turn a
// per-request base counter into per-thread world seeds directly on the GPU (--seed-gen),
// skipping the Java-side generate -> write-to-file -> C++-read-from-file round trip
// entirely for continuous-mode runs. This does NOT need to match any specific Java RNG
// output bit-for-bit -- it only needs to be deterministic and well-distributed, which a
// standard splitmix64 step already guarantees on its own.
DC_HD inline long long dev_splitmix64(unsigned long long x) {
    x += 0x9E3779B97F4A7C15ULL;
    unsigned long long z = x;
    z = (z ^ (z >> 30)) * 0xBF58476D1CE4E5B9ULL;
    z = (z ^ (z >> 27)) * 0x94D049BB133111EBULL;
    z = z ^ (z >> 31);
    return (long long)z;
}

// Precomputed "octave_N" digests (N = -1..-16), indexed by (-octave - 1) so octave=-1 is
// index 0 and octave=-16 is index 15 -- covers every octave index used by any key in this
// system (verified: cave_entrance/spaghetti_*/erosion/temperature/continentalness/ridge/
// cave_layer/cave_cheese/aquifer/offset/jagged all fall within -16..-1).
DC_HD inline const uint8_t* dev_octaveDigest(int octave) {
    static const uint8_t* const table[16] = {
        DIGEST_OCTAVE_NEG1, DIGEST_OCTAVE_NEG2, DIGEST_OCTAVE_NEG3, DIGEST_OCTAVE_NEG4,
        DIGEST_OCTAVE_NEG5, DIGEST_OCTAVE_NEG6, DIGEST_OCTAVE_NEG7, DIGEST_OCTAVE_NEG8,
        DIGEST_OCTAVE_NEG9, DIGEST_OCTAVE_NEG10, DIGEST_OCTAVE_NEG11, DIGEST_OCTAVE_NEG12,
        DIGEST_OCTAVE_NEG13, DIGEST_OCTAVE_NEG14, DIGEST_OCTAVE_NEG15, DIGEST_OCTAVE_NEG16,
    };
    int idx = -octave - 1;
    return table[idx];
}

// [mirrors OctavePerlinNoiseSampler::create] `random` mutated in place via its own
// createRandomDeriver(), then each octave's PerlinNoiseSampler is derived from that
// deriver's CURRENT (unmutated per-call) state + the octave's precomputed digest --
// createRandom()/createRandomFromDigest() are pure functions of the deriver's state, so
// this matches calling deriver.createRandom("octave_"+n) for each n independently.
DC_HD inline void dev_buildOctave(Xoroshiro128PP& random, int firstOctave,
                                  const double* amplitudes, int count, DevOctave* o) {
    o->octaveCount = count;
    o->lacunarity = pow(2.0, firstOctave);
    o->persistence = pow(2.0, count - 1) / (pow(2.0, count) - 1.0);

    Xoroshiro128PP octaveDeriver = random.createRandomDeriver();
    double lf = o->lacunarity, la = o->persistence;
    for (int i = 0; i < count; i++) {
        o->amplitudes[i] = amplitudes[i];
        o->hasOctave[i] = (amplitudes[i] != 0.0);
        // Must match the host toDev() walk exactly, or a GPU-built cache and a host-built
        // one would sample differently.
        o->freqAt[i] = lf; o->ampAt[i] = la;
        lf *= 2.0; la /= 2.0;
        if (o->hasOctave[i]) {
            int octave = firstOctave + i;
            Xoroshiro128PP perOctaveRandom = Xoroshiro128PP::createRandomFromDigest(octaveDeriver, dev_octaveDigest(octave));
            o->samplers[i] = toDev(PerlinNoiseSampler::create(perOctaveRandom));
        }
    }
}

// [mirrors DoublePerlinNoiseSampler::create] `random` taken BY VALUE and threaded through
// both calls -- the first buildOctave's internal createRandomDeriver() mutates this local
// copy, and the second call sees that mutated state (matching the host's by-value+reuse
// semantics exactly).
DC_HD inline void dev_buildDoublePerlin(Xoroshiro128PP random, int firstOctave,
                                        const double* amplitudes, int count, DevDoublePerlin* d) {
    dev_buildOctave(random, firstOctave, amplitudes, count, &d->first);
    dev_buildOctave(random, firstOctave, amplitudes, count, &d->second);

    int minOctave = 2147483647, maxOctave = -2147483648;
    for (int i = 0; i < count; i++) {
        if (amplitudes[i] == 0.0) continue;
        if (i < minOctave) minOctave = i;
        if (i > maxOctave) maxOctave = i;
    }
    double createAmp_n = maxOctave - minOctave;
    double createAmplitude = 0.1 * (1.0 + 1.0 / (createAmp_n + 1.0));
    d->amplitude = 0.16666666666666666 / createAmplitude;
}

// [mirrors noise_setup.h's makeSampler] deriver.createRandom("minecraft:"+key) -> a fresh
// Xoroshiro128PP (deriver itself unchanged, matches createRandom's const-ness) -> build.
DC_HD inline void dev_makeNamedSampler(const Xoroshiro128PP& deriver, const uint8_t* keyDigest,
                                       int firstOctave, const double* amplitudes, int count,
                                       DevDoublePerlin* d) {
    Xoroshiro128PP r = Xoroshiro128PP::createRandomFromDigest(deriver, keyDigest);
    dev_buildDoublePerlin(r, firstOctave, amplitudes, count, d);
}

// [mirrors WorldNoiseCache::build] 3 independent derivers from the SAME world seed (not
// chained), 14 named samplers across them in this exact order.
DC_HD inline void dev_buildWorldNoiseCache(int64_t worldSeed, DevNoiseCache* c) {
    Xoroshiro128PP base = Xoroshiro128PP::fromSingleSeed(worldSeed);
    Xoroshiro128PP deriver = base.createRandomDeriver();
    { const double amp[] = {0.4, 0.5, 1.0}; dev_makeNamedSampler(deriver, DIGEST_cave_entrance, -7, amp, 3, &c->caveEntrance); }
    { const double amp[] = {1.0}; dev_makeNamedSampler(deriver, DIGEST_spaghetti_3d_rarity, -11, amp, 1, &c->spaghettiRarity); }
    { const double amp[] = {1.0}; dev_makeNamedSampler(deriver, DIGEST_spaghetti_3d_thickness, -8, amp, 1, &c->spaghettiThickness); }
    { const double amp[] = {1.0}; dev_makeNamedSampler(deriver, DIGEST_spaghetti_3d_1, -7, amp, 1, &c->spaghetti3D1); }
    { const double amp[] = {1.0}; dev_makeNamedSampler(deriver, DIGEST_spaghetti_3d_2, -7, amp, 1, &c->spaghetti3D2); }
    { const double amp[] = {1.0}; dev_makeNamedSampler(deriver, DIGEST_spaghetti_roughness_modulator, -8, amp, 1, &c->spaghettiRoughnessModulator); }
    { const double amp[] = {1.0}; dev_makeNamedSampler(deriver, DIGEST_spaghetti_roughness, -5, amp, 1, &c->spaghettiRoughness); }
    { const double amp[] = {1.0, 1.0, 0.0, 1.0, 1.0}; dev_makeNamedSampler(deriver, DIGEST_erosion, -9, amp, 5, &c->erosion); }
    { const double amp[] = {1.5, 0.0, 1.0, 0.0, 0.0, 0.0}; dev_makeNamedSampler(deriver, DIGEST_temperature, -10, amp, 6, &c->temperature); }
    { const double amp[] = {1.0, 1.0, 2.0, 2.0, 2.0, 1.0, 1.0, 1.0, 1.0}; dev_makeNamedSampler(deriver, DIGEST_continentalness, -9, amp, 9, &c->continentalness); }
    { const double amp[] = {1.0, 2.0, 1.0, 0.0, 0.0, 0.0}; dev_makeNamedSampler(deriver, DIGEST_ridge, -7, amp, 6, &c->ridge); }

    Xoroshiro128PP cheeseBase = Xoroshiro128PP::fromSingleSeed(worldSeed);
    Xoroshiro128PP cheeseDeriver = cheeseBase.createRandomDeriver();
    { const double amp[] = {1.0}; dev_makeNamedSampler(cheeseDeriver, DIGEST_cave_layer, -8, amp, 1, &c->caveLayer); }
    { const double amp[] = {0.5, 1.0, 2.0, 1.0, 2.0, 1.0, 0.0, 2.0, 0.0}; dev_makeNamedSampler(cheeseDeriver, DIGEST_cave_cheese, -8, amp, 9, &c->caveCheese); }

    Xoroshiro128PP aqBase = Xoroshiro128PP::fromSingleSeed(worldSeed);
    Xoroshiro128PP aqDeriver = aqBase.createRandomDeriver();
    { const double amp[] = {1.0}; dev_makeNamedSampler(aqDeriver, DIGEST_aquifer_fluid_level_floodedness, -7, amp, 1, &c->aquiferFloodedness); }
}

// [mirrors WorldNoiseCache::build] Builds the OTHER 11 named samplers dev_buildWorldNoiseCache
// builds above -- everything except erosion/temperature/ridge (the climate trio, built
// separately and unconditionally in constructClimateKernel; see DevClimateCache's comment).
// Safe to split this way for the exact same reason the earlier constructClimateKernel split
// is safe: dev_makeNamedSampler takes `deriver` by const& and is a pure function of
// (deriver-state, digest) -- every named sampler off the SAME deriver is independent of
// every other one, so building any subset (in any order) gives bit-identical results to
// building the full set and discarding the rest. Caller is responsible for copying the
// climate trio in from the already-built DevClimateCache -- this function does not touch
// c->erosion/c->temperature/c->ridge at all.
DC_HD inline void dev_buildWorldNoiseCacheRemaining(int64_t worldSeed, DevNoiseCache* c) {
    Xoroshiro128PP base = Xoroshiro128PP::fromSingleSeed(worldSeed);
    Xoroshiro128PP deriver = base.createRandomDeriver();
    { const double amp[] = {0.4, 0.5, 1.0}; dev_makeNamedSampler(deriver, DIGEST_cave_entrance, -7, amp, 3, &c->caveEntrance); }
    { const double amp[] = {1.0}; dev_makeNamedSampler(deriver, DIGEST_spaghetti_3d_rarity, -11, amp, 1, &c->spaghettiRarity); }
    { const double amp[] = {1.0}; dev_makeNamedSampler(deriver, DIGEST_spaghetti_3d_thickness, -8, amp, 1, &c->spaghettiThickness); }
    { const double amp[] = {1.0}; dev_makeNamedSampler(deriver, DIGEST_spaghetti_3d_1, -7, amp, 1, &c->spaghetti3D1); }
    { const double amp[] = {1.0}; dev_makeNamedSampler(deriver, DIGEST_spaghetti_3d_2, -7, amp, 1, &c->spaghetti3D2); }
    { const double amp[] = {1.0}; dev_makeNamedSampler(deriver, DIGEST_spaghetti_roughness_modulator, -8, amp, 1, &c->spaghettiRoughnessModulator); }
    { const double amp[] = {1.0}; dev_makeNamedSampler(deriver, DIGEST_spaghetti_roughness, -5, amp, 1, &c->spaghettiRoughness); }
    { const double amp[] = {1.0, 1.0, 2.0, 2.0, 2.0, 1.0, 1.0, 1.0, 1.0}; dev_makeNamedSampler(deriver, DIGEST_continentalness, -9, amp, 9, &c->continentalness); }
    // erosion/temperature/ridge intentionally SKIPPED -- caller already has them.

    Xoroshiro128PP cheeseBase = Xoroshiro128PP::fromSingleSeed(worldSeed);
    Xoroshiro128PP cheeseDeriver = cheeseBase.createRandomDeriver();
    { const double amp[] = {1.0}; dev_makeNamedSampler(cheeseDeriver, DIGEST_cave_layer, -8, amp, 1, &c->caveLayer); }
    { const double amp[] = {0.5, 1.0, 2.0, 1.0, 2.0, 1.0, 0.0, 2.0, 0.0}; dev_makeNamedSampler(cheeseDeriver, DIGEST_cave_cheese, -8, amp, 9, &c->caveCheese); }

    Xoroshiro128PP aqBase = Xoroshiro128PP::fromSingleSeed(worldSeed);
    Xoroshiro128PP aqDeriver = aqBase.createRandomDeriver();
    { const double amp[] = {1.0}; dev_makeNamedSampler(aqDeriver, DIGEST_aquifer_fluid_level_floodedness, -7, amp, 1, &c->aquiferFloodedness); }
}

// Builds ONLY the climate trio -- see DevClimateCache's comment for why. Mirrors the
// erosion/temperature/ridge portion of dev_buildWorldNoiseCache exactly (same base
// deriver, same digests/octaves/amplitudes).
// Staged variants of dev_buildClimateCache. The three climate samplers are derived
// INDEPENDENTLY from the seed (each via createRandomFromDigest with its own key -- no
// sequential RNG state is shared), so building a subset produces bit-identical results to
// building all three. That lets the caller build only `erosion`, reject on it, and never
// pay for temperature/ridge on the seeds that fail -- which is the overwhelming majority.
// Splitting is only worthwhile because sampler construction is ~97% of total GPU time.
DC_HD inline void dev_buildClimateErosion(int64_t worldSeed, DevClimateCache* c) {
    Xoroshiro128PP base = Xoroshiro128PP::fromSingleSeed(worldSeed);
    Xoroshiro128PP deriver = base.createRandomDeriver();
    const double amp[] = {1.0, 1.0, 0.0, 1.0, 1.0};
    dev_makeNamedSampler(deriver, DIGEST_erosion, -9, amp, 5, &c->erosion);
}

DC_HD inline void dev_buildClimateTempRidge(int64_t worldSeed, DevClimateCache* c) {
    Xoroshiro128PP base = Xoroshiro128PP::fromSingleSeed(worldSeed);
    Xoroshiro128PP deriver = base.createRandomDeriver();
    { const double amp[] = {1.5, 0.0, 1.0, 0.0, 0.0, 0.0}; dev_makeNamedSampler(deriver, DIGEST_temperature, -10, amp, 6, &c->temperature); }
    { const double amp[] = {1.0, 2.0, 1.0, 0.0, 0.0, 0.0}; dev_makeNamedSampler(deriver, DIGEST_ridge, -7, amp, 6, &c->ridge); }
}

DC_HD inline void dev_buildClimateCache(int64_t worldSeed, DevClimateCache* c) {
    Xoroshiro128PP base = Xoroshiro128PP::fromSingleSeed(worldSeed);
    Xoroshiro128PP deriver = base.createRandomDeriver();
    { const double amp[] = {1.0, 1.0, 0.0, 1.0, 1.0}; dev_makeNamedSampler(deriver, DIGEST_erosion, -9, amp, 5, &c->erosion); }
    { const double amp[] = {1.5, 0.0, 1.0, 0.0, 0.0, 0.0}; dev_makeNamedSampler(deriver, DIGEST_temperature, -10, amp, 6, &c->temperature); }
    { const double amp[] = {1.0, 2.0, 1.0, 0.0, 0.0, 0.0}; dev_makeNamedSampler(deriver, DIGEST_ridge, -7, amp, 6, &c->ridge); }
}

// [mirrors LegacyOctave::create] ONE shared, sequentially-mutating generator constructs
// PerlinNoiseSamplers DIRECTLY (no name-hashing at all) in descending index order, with a
// single unconditional PerlinNoiseSampler::create() up front. All 3 callers here use
// all-1.0 amplitudes, so the "amplitude==0" skip path is unreachable but kept for parity.
DC_HD inline void dev_buildLegacyOctave(Xoroshiro128PP& random, int firstOctaveNeg, int size, DevLegacyOctave* o) {
    o->octaveCount = size;
    o->lacunarity = pow(2.0, firstOctaveNeg);
    o->persistence = pow(2.0, size - 1) / (pow(2.0, size) - 1.0);
    for (int i = 0; i < size; i++) o->hasOctave[i] = false;

    int negFirstOctave = -firstOctaveNeg;
    DevPerlin singleSampler = toDev(PerlinNoiseSampler::create(random)); // unconditional
    if (negFirstOctave >= 0 && negFirstOctave < size) {
        o->samplers[negFirstOctave] = singleSampler;
        o->hasOctave[negFirstOctave] = true;
    }
    for (int i = negFirstOctave - 1; i >= 0; i--) {
        o->samplers[i] = toDev(PerlinNoiseSampler::create(random));
        o->hasOctave[i] = true;
    }
}

// [mirrors InterpolatedNoiseSampler::create] `random` taken by value, mutated
// sequentially across all 3 createLegacy calls (matching the single shared generator
// passed through Java's 3-arg constructor).
DC_HD inline void dev_buildInterpolatedNoiseSampler(Xoroshiro128PP random, int cellWidth, int cellHeight, DevInterpolatedNoiseSampler* s) {
    s->xzScale = 684.412; s->yScale = 684.412;
    s->xzMainScale = s->xzScale / 80.0;
    s->yMainScale = s->yScale / 160.0;
    s->cellWidth = cellWidth; s->cellHeight = cellHeight;
    dev_buildLegacyOctave(random, -15, 16, &s->lowerInterpolatedNoise);
    dev_buildLegacyOctave(random, -15, 16, &s->upperInterpolatedNoise);
    dev_buildLegacyOctave(random, -7, 8, &s->interpolationNoise);
}

// [mirrors DensityContext::build] shiftNoise/jaggedNoise/terrainNoise, all derived from
// ONE deriver off the world seed. terrainParams is deliberately NOT built here -- it's
// entirely seed-independent (vanilla constants only), built once on the host and shared
// across every seed (see density_prefilter.h's DensityContext::build comment) -- the
// caller fills devDensityCtx.terrainParams from that shared, already-flattened table.
DC_HD inline void dev_buildDensityContextSeeded(int64_t seed, DevDoublePerlin* outShift,
                                                DevDoublePerlin* outJagged,
                                                DevInterpolatedNoiseSampler* outTerrain) {
    Xoroshiro128PP base = Xoroshiro128PP::fromSingleSeed(seed);
    Xoroshiro128PP deriver = base.createRandomDeriver();

    { const double amp[] = {1.0, 1.0, 1.0, 0.0};
      Xoroshiro128PP r = Xoroshiro128PP::createRandomFromDigest(deriver, DIGEST_offset);
      dev_buildDoublePerlin(r, -3, amp, 4, outShift); }
    { double amp[16]; for (int i = 0; i < 16; i++) amp[i] = 1.0;
      Xoroshiro128PP r = Xoroshiro128PP::createRandomFromDigest(deriver, DIGEST_jagged);
      dev_buildDoublePerlin(r, -16, amp, 16, outJagged); }
    { Xoroshiro128PP r = Xoroshiro128PP::createRandomFromDigest(deriver, DIGEST_terrain);
      dev_buildInterpolatedNoiseSampler(r, 4, 8, outTerrain); }
}

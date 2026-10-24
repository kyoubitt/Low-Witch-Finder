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

DC_HD inline long long dev_splitmix64(unsigned long long x) {
    x += 0x9E3779B97F4A7C15ULL;
    unsigned long long z = x;
    z = (z ^ (z >> 30)) * 0xBF58476D1CE4E5B9ULL;
    z = (z ^ (z >> 27)) * 0x94D049BB133111EBULL;
    z = z ^ (z >> 31);
    return (long long)z;
}

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

        o->freqAt[i] = lf; o->ampAt[i] = la;
        lf *= 2.0; la /= 2.0;
        if (o->hasOctave[i]) {
            int octave = firstOctave + i;
            Xoroshiro128PP perOctaveRandom = Xoroshiro128PP::createRandomFromDigest(octaveDeriver, dev_octaveDigest(octave));
            { PerlinNoiseSampler p = PerlinNoiseSampler::create(perOctaveRandom); dev_fillPerlin(o->samplers[i], p); }
        }
    }
}

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

DC_HD inline void dev_makeNamedSampler(const Xoroshiro128PP& deriver, const uint8_t* keyDigest,
                                       int firstOctave, const double* amplitudes, int count,
                                       DevDoublePerlin* d) {
    Xoroshiro128PP r = Xoroshiro128PP::createRandomFromDigest(deriver, keyDigest);
    dev_buildDoublePerlin(r, firstOctave, amplitudes, count, d);
}

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

    Xoroshiro128PP cheeseBase = Xoroshiro128PP::fromSingleSeed(worldSeed);
    Xoroshiro128PP cheeseDeriver = cheeseBase.createRandomDeriver();
    { const double amp[] = {1.0}; dev_makeNamedSampler(cheeseDeriver, DIGEST_cave_layer, -8, amp, 1, &c->caveLayer); }
    { const double amp[] = {0.5, 1.0, 2.0, 1.0, 2.0, 1.0, 0.0, 2.0, 0.0}; dev_makeNamedSampler(cheeseDeriver, DIGEST_cave_cheese, -8, amp, 9, &c->caveCheese); }

    Xoroshiro128PP aqBase = Xoroshiro128PP::fromSingleSeed(worldSeed);
    Xoroshiro128PP aqDeriver = aqBase.createRandomDeriver();
    { const double amp[] = {1.0}; dev_makeNamedSampler(aqDeriver, DIGEST_aquifer_fluid_level_floodedness, -7, amp, 1, &c->aquiferFloodedness); }
}

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

DC_HD inline void dev_buildLegacyOctave(Xoroshiro128PP& random, int firstOctaveNeg, int size, DevLegacyOctave* o) {
    o->octaveCount = size;
    o->lacunarity = pow(2.0, firstOctaveNeg);
    o->persistence = pow(2.0, size - 1) / (pow(2.0, size) - 1.0);
    for (int i = 0; i < size; i++) o->hasOctave[i] = false;

    int negFirstOctave = -firstOctaveNeg;
    DevPerlin singleSampler = toDev(PerlinNoiseSampler::create(random));
    if (negFirstOctave >= 0 && negFirstOctave < size) {
        o->samplers[negFirstOctave] = singleSampler;
        o->hasOctave[negFirstOctave] = true;
    }
    for (int i = negFirstOctave - 1; i >= 0; i--) {
        { PerlinNoiseSampler p = PerlinNoiseSampler::create(random); dev_fillPerlin(o->samplers[i], p); }
        o->hasOctave[i] = true;
    }
}

DC_HD inline void dev_buildInterpolatedNoiseSampler(Xoroshiro128PP random, int cellWidth, int cellHeight, DevInterpolatedNoiseSampler* s) {
    s->xzScale = 684.412; s->yScale = 684.412;
    s->xzMainScale = s->xzScale / 80.0;
    s->yMainScale = s->yScale / 160.0;
    s->cellWidth = cellWidth; s->cellHeight = cellHeight;
    dev_buildLegacyOctave(random, -15, 16, &s->lowerInterpolatedNoise);
    dev_buildLegacyOctave(random, -15, 16, &s->upperInterpolatedNoise);
    dev_buildLegacyOctave(random, -7, 8, &s->interpolationNoise);
}

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

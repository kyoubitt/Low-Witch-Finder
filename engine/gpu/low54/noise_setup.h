// Builds the exact set of noise samplers SearchCoords.WorldNoiseCache creates, and
// implements check()'s Entrance/Entrance2/Cheese formulas on top of them.
//
// [bytecode-verified] The construction ORDER below matters and is not arbitrary:
// WorldNoiseCache derives 11 samplers from one deriver in sequence, then 2 more from a
// SECOND independent deriver (cheeseDeriver), then 1 more from a THIRD. Because
// createRandomDeriver() mutates its generator, changing this order changes every value.
#pragma once
#include "xoroshiro128pp.h"
#include "perlin_noise.h"
#include <string>
#include <vector>

// [bytecode-verified] NoiseRegistry tables (firstOctave, amplitudes) for exactly the keys
// SearchCoords uses, dumped from the live registry via NoiseReferenceVectorGenerator.
struct NoiseParams { int firstOctave; std::vector<double> amplitudes; };

inline NoiseParams NP_CAVE_ENTRANCE()      { return {-7, {0.4, 0.5, 1.0}}; }
inline NoiseParams NP_SPAGHETTI_3D_RARITY(){ return {-11, {1.0}}; }
inline NoiseParams NP_SPAGHETTI_3D_THICK() { return {-8, {1.0}}; }
inline NoiseParams NP_SPAGHETTI_3D_1()     { return {-7, {1.0}}; }
inline NoiseParams NP_SPAGHETTI_3D_2()     { return {-7, {1.0}}; }
inline NoiseParams NP_SPAG_ROUGH_MOD()     { return {-8, {1.0}}; }
inline NoiseParams NP_SPAG_ROUGH()         { return {-5, {1.0}}; }
inline NoiseParams NP_EROSION()            { return {-9, {1.0, 1.0, 0.0, 1.0, 1.0}}; }
inline NoiseParams NP_TEMPERATURE()        { return {-10, {1.5, 0.0, 1.0, 0.0, 0.0, 0.0}}; }
inline NoiseParams NP_CONTINENTALNESS()    { return {-9, {1.0, 1.0, 2.0, 2.0, 2.0, 1.0, 1.0, 1.0, 1.0}}; }
inline NoiseParams NP_RIDGE()              { return {-7, {1.0, 2.0, 1.0, 0.0, 0.0, 0.0}}; }
inline NoiseParams NP_CAVE_LAYER()         { return {-8, {1.0}}; }
inline NoiseParams NP_CAVE_CHEESE()        { return {-8, {0.5, 1.0, 2.0, 1.0, 2.0, 1.0, 0.0, 2.0, 0.0}}; }
inline NoiseParams NP_AQUIFER_FLOODEDNESS(){ return {-7, {1.0}}; }

// [bytecode-verified] LazyDoublePerlinNoiseSampler.createNoiseSampler resolves the
// identifier as "minecraft:" + key.value (e.g. "minecraft:erosion"), then
// DoublePerlinNoiseSampler.create(deriver.createRandom(identifier), params).
inline DoublePerlinNoiseSampler makeSampler(const Xoroshiro128PP& deriver,
                                            const std::string& keyValue,
                                            const NoiseParams& p) {
    Xoroshiro128PP r = deriver.createRandom("minecraft:" + keyValue);
    return DoublePerlinNoiseSampler::create(r, p.firstOctave, p.amplitudes);
}

// Mirrors SearchCoords.WorldNoiseCache for WorldPresetMode.NORMAL.
struct WorldNoiseCache {
    DoublePerlinNoiseSampler caveEntrance, spaghettiRarity, spaghettiThickness;
    DoublePerlinNoiseSampler spaghetti3D1, spaghetti3D2;
    DoublePerlinNoiseSampler spaghettiRoughnessModulator, spaghettiRoughness;
    DoublePerlinNoiseSampler erosion, temperature, continentalness, ridge;
    DoublePerlinNoiseSampler caveLayer, caveCheese, aquiferFloodedness;

    static WorldNoiseCache build(int64_t worldSeed) {
        WorldNoiseCache c;
        // Deriver #1 -- 11 samplers, in this exact order.
        Xoroshiro128PP base = Xoroshiro128PP::fromSingleSeed(worldSeed);
        Xoroshiro128PP deriver = base.createRandomDeriver();
        c.caveEntrance      = makeSampler(deriver, "cave_entrance", NP_CAVE_ENTRANCE());
        c.spaghettiRarity   = makeSampler(deriver, "spaghetti_3d_rarity", NP_SPAGHETTI_3D_RARITY());
        c.spaghettiThickness= makeSampler(deriver, "spaghetti_3d_thickness", NP_SPAGHETTI_3D_THICK());
        c.spaghetti3D1      = makeSampler(deriver, "spaghetti_3d_1", NP_SPAGHETTI_3D_1());
        c.spaghetti3D2      = makeSampler(deriver, "spaghetti_3d_2", NP_SPAGHETTI_3D_2());
        c.spaghettiRoughnessModulator = makeSampler(deriver, "spaghetti_roughness_modulator", NP_SPAG_ROUGH_MOD());
        c.spaghettiRoughness= makeSampler(deriver, "spaghetti_roughness", NP_SPAG_ROUGH());
        c.erosion           = makeSampler(deriver, "erosion", NP_EROSION());
        c.temperature       = makeSampler(deriver, "temperature", NP_TEMPERATURE());
        c.continentalness   = makeSampler(deriver, "continentalness", NP_CONTINENTALNESS());
        c.ridge             = makeSampler(deriver, "ridge", NP_RIDGE());

        // Deriver #2 -- a fresh generator from the same world seed (NOT a continuation).
        Xoroshiro128PP cheeseBase = Xoroshiro128PP::fromSingleSeed(worldSeed);
        Xoroshiro128PP cheeseDeriver = cheeseBase.createRandomDeriver();
        c.caveLayer  = makeSampler(cheeseDeriver, "cave_layer", NP_CAVE_LAYER());
        c.caveCheese = makeSampler(cheeseDeriver, "cave_cheese", NP_CAVE_CHEESE());

        // Deriver #3 -- again fresh from the world seed.
        Xoroshiro128PP aqBase = Xoroshiro128PP::fromSingleSeed(worldSeed);
        Xoroshiro128PP aqDeriver = aqBase.createRandomDeriver();
        c.aquiferFloodedness = makeSampler(aqDeriver, "aquifer_fluid_level_floodedness", NP_AQUIFER_FLOODEDNESS());
        return c;
    }
};

// ---- helpers matching the noise-sampler library's math ----
PN_HD inline double mh_clamp(double v, double lo, double hi) { return v < lo ? lo : (v > hi ? hi : v); }
PN_HD inline double mh_clampedLerp(double a, double b, double t) {
    if (t < 0.0) return a;
    if (t > 1.0) return b;
    return pn_lerp(t, a, b);
}
// NoiseColumnSampler.CaveScaler.scaleTunnels
PN_HD inline double scaleTunnels(double d) {
    return d < -0.5 ? 0.75 : (d < 0.0 ? 1.0 : (d < 0.5 ? 1.5 : 2.0));
}
// Util.lerpFromProgress(sampler, x, y, z, lo, hi)
//   -> MathHelper.lerpFromProgress(sample, -1.0, 1.0, lo, hi)
//   -> lerp(getLerpProgress(sample, -1, 1), lo, hi)
// [bytecode-verified] This is PLAIN lerp, NOT clampedLerp -- an earlier draft here
// wrongly used the clamping variant, which would have silently altered every
// spaghetti-thickness value whose noise fell outside [-1,1].
PN_HD inline double lerpFromProgress(const DoublePerlinNoiseSampler& s, double x, double y, double z,
                                      double lo, double hi) {
    double v = s.sample(x, y, z);
    double progress = (v - (-1.0)) / (1.0 - (-1.0));
    return pn_lerp(progress, lo, hi);
}
// NoiseColumnSampler.sample(sampler, x, y, z, scale)
PN_HD inline double sampleScaled(const DoublePerlinNoiseSampler& s, double x, double y, double z, double scale) {
    return s.sample(x / scale, y / scale, z / scale);
}

// ---- SearchCoords.Entrance / Entrance2 / Cheese ----
PN_HD inline double Entrance(const WorldNoiseCache& c, int x, int y, int z) {
    double cc = c.caveEntrance.sample(x * 0.75, y * 0.5, z * 0.75) + 0.37
              + mh_clampedLerp(0.3, 0.0, (10 + (double)y) / 40.0);
    double d = c.spaghettiRarity.sample(x * 2.0, y, z * 2.0);
    double e = scaleTunnels(d);
    double h = lerpFromProgress(c.spaghettiThickness, x, y, z, 0.065, 0.088);
    double l = sampleScaled(c.spaghetti3D1, x, y, z, e);
    double m = fabs(e * l) - h;
    double n = sampleScaled(c.spaghetti3D2, x, y, z, e);
    double o = fabs(e * n) - h;
    double p = mh_clamp(m > o ? m : o, -1.0, 1.0);
    double q = (-0.05 + (-0.05 * c.spaghettiRoughnessModulator.sample(x, y, z)))
             * (-0.4 + fabs(c.spaghettiRoughness.sample(x, y, z)));
    double pq = p + q;
    return cc < pq ? cc : pq;
}

PN_HD inline double Entrance2(const WorldNoiseCache& c, int x, int y, int z) {
    double d = c.spaghettiRarity.sample(x * 2.0, y, z * 2.0);
    double e = scaleTunnels(d);
    double h = lerpFromProgress(c.spaghettiThickness, x, y, z, 0.065, 0.088);
    double l = sampleScaled(c.spaghetti3D1, x, y, z, e);
    double m = fabs(e * l) - h;
    double n = sampleScaled(c.spaghetti3D2, x, y, z, e);
    double o = fabs(e * n) - h;
    double p = mh_clamp(m > o ? m : o, -1.0, 1.0);
    double q = (-0.05 + (-0.05 * c.spaghettiRoughnessModulator.sample(x, y, z)))
             * (-0.4 + fabs(c.spaghettiRoughness.sample(x, y, z)));
    return p + q;
}

PN_HD inline double Cheese(const WorldNoiseCache& c, int x, int y, int z) {
    double layer = c.caveLayer.sample(x, y * 8.0, z);
    double a = 4.0 * layer * layer;
    double b = mh_clamp(0.27 + c.caveCheese.sample(x, y * 0.6666666666666666, z), -1.0, 1.0);
    return a + b;
}

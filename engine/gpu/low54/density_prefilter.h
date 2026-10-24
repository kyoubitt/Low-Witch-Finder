// Port of SearchCoords.passesDensityPrefilter and its dependency chain
// (estimateCheeseSurfaceLadder -> cheeseWithSloped -> sampleSlopedCheese ->
// NoiseColumnSampler). Every formula/constant marked [bytecode-verified] was pulled
// from javap -c of the actual noise-sampler jar; the rest (isHutRotated90,
// mapHutLocalToWorld) already existed in swamphut.h / this project's other headers.
#pragma once
#include "xoroshiro128pp.h"
#include "perlin_noise.h"
#include "noise_setup.h"
#include "terrain_shaper.h"
#include "swamphut.h"
#include <vector>
#include <cmath>

#ifdef __CUDACC__
#define DP_HD __host__ __device__
#else
#define DP_HD
#endif

// Slack in the density prefilter accept test. Larger = safer but more candidates for
// Phase 2 to chew through, and Phase 2 is the binding constraint at the -50 floor.
#ifndef DENSITY_MARGIN
#define DENSITY_MARGIN 8
#endif

inline int javaFloorDiv(int a, int b) {
    int q = a / b;
    if ((a % b != 0) && ((a < 0) != (b < 0))) q--;
    return q;
}

// [bytecode-verified] OctavePerlinNoiseSampler's "legacy" (createLegacy) construction
// path -- genuinely different from the named-per-octave derivation already used
// elsewhere: ONE shared, sequentially-mutating Xoroshiro128PP constructs
// PerlinNoiseSamplers directly (no createRandom(name) at all), in strict descending
// index order from negFirstOctave down to 0. Used only by InterpolatedNoiseSampler's
// three internal octave groups. All three calls here use all-1.0 amplitudes, so the
// "amplitude==0 -> consumeCount(262) instead" skip path never triggers for us, but is
// implemented anyway for faithfulness.
struct LegacyOctave {
    int firstOctave;
    std::vector<double> amplitudes;
    double lacunarity = 0, persistence = 0;
    std::vector<bool> hasOctave;
    std::vector<PerlinNoiseSampler> octaveSamplers;

    static LegacyOctave create(Xoroshiro128PP& random, int firstOctaveNeg, int size) {
        LegacyOctave o;
        o.firstOctave = firstOctaveNeg;
        o.amplitudes.assign(size, 1.0);
        o.lacunarity = pow(2.0, firstOctaveNeg);
        o.persistence = pow(2.0, size - 1) / (pow(2.0, size) - 1.0);
        o.hasOctave.assign(size, false);
        o.octaveSamplers.resize(size);

        int negFirstOctave = -firstOctaveNeg;
        PerlinNoiseSampler singleSampler = PerlinNoiseSampler::create(random); // unconditional
        if (negFirstOctave >= 0 && negFirstOctave < size && o.amplitudes[negFirstOctave] != 0.0) {
            o.octaveSamplers[negFirstOctave] = singleSampler;
            o.hasOctave[negFirstOctave] = true;
        }
        for (int i = negFirstOctave - 1; i >= 0; i--) {
            if (o.amplitudes[i] != 0.0) {
                o.octaveSamplers[i] = PerlinNoiseSampler::create(random);
                o.hasOctave[i] = true;
            }
            // else: random.consumeCount(262) in Java -- never hit here (no zero amplitudes)
        }
        return o;
    }

    // [bytecode-verified] OctavePerlinNoiseSampler.getOctave(i) is NOT a direct index --
    // it returns octaveSamplers[length-1-i]. This is the public accessor
    // InterpolatedNoiseSampler.calculateNoise actually calls (not the class's own
    // internal sample(), which indexes directly and needs no such correction -- that's
    // why the already-validated check()-path octaves were unaffected by this).
    const PerlinNoiseSampler* getOctave(int i) const {
        int idx = (int)amplitudes.size() - 1 - i;
        return hasOctave[idx] ? &octaveSamplers[idx] : nullptr;
    }
};

// [bytecode-verified] InterpolatedNoiseSampler.calculateNoise -- matches vanilla's known
// "BlendedNoise": 684.412 xzScale/yScale, /80 and /160 main-scale divisors, 8+16 octave
// loop, clampedLerp blend by t=((interpNoiseSum/10)+1)/2, final /512 and /128 divisors.
struct InterpolatedNoiseSampler {
    LegacyOctave lowerInterpolatedNoise, upperInterpolatedNoise, interpolationNoise;
    double xzScale = 684.412, yScale = 684.412, xzMainScale, yMainScale;
    int cellWidth, cellHeight;

    // `random` taken by value: mutated sequentially across all 3 createLegacy calls
    // below, matching Java's single shared generator passed through the 3-arg ctor.
    static InterpolatedNoiseSampler create(Xoroshiro128PP random, int cellWidth_, int cellHeight_) {
        InterpolatedNoiseSampler s;
        s.xzMainScale = s.xzScale / 80.0;
        s.yMainScale = s.yScale / 160.0;
        s.cellWidth = cellWidth_;
        s.cellHeight = cellHeight_;
        s.lowerInterpolatedNoise = LegacyOctave::create(random, -15, 16);
        s.upperInterpolatedNoise = LegacyOctave::create(random, -15, 16);
        s.interpolationNoise = LegacyOctave::create(random, -7, 8);
        return s;
    }

    double calculateNoise(int x, int y, int z) const {
        int cellX = javaFloorDiv(x, cellWidth);
        int cellY = javaFloorDiv(y, cellHeight);
        int cellZ = javaFloorDiv(z, cellWidth);

        double d = 0.0, amp = 1.0;
        for (int i = 0; i < 8; i++) {
            const PerlinNoiseSampler* oct = interpolationNoise.getOctave(i);
            if (oct) {
                double fx = pn_maintainPrecision(cellX * xzMainScale * amp);
                double fy = pn_maintainPrecision(cellY * yMainScale * amp);
                double fz = pn_maintainPrecision(cellZ * xzMainScale * amp);
                double yScaleArg = yMainScale * amp;
                double yMaxArg = cellY * yMainScale * amp;
                d += oct->sample(fx, fy, fz, yScaleArg, yMaxArg) / amp;
            }
            amp /= 2.0;
        }
        double t = ((d / 10.0) + 1.0) / 2.0;
        bool useUpperOnly = t >= 1.0;
        bool useLowerOnly = t <= 0.0;

        double lowerSum = 0.0, upperSum = 0.0;
        amp = 1.0;
        for (int i = 0; i < 16; i++) {
            double fx = pn_maintainPrecision(cellX * xzScale * amp);
            double fy = pn_maintainPrecision(cellY * yScale * amp);
            double fz = pn_maintainPrecision(cellZ * xzScale * amp);
            double yScaleArg2 = yScale * amp;
            double yMaxArg2 = cellY * yScaleArg2;
            if (!useUpperOnly) {
                const PerlinNoiseSampler* oct = lowerInterpolatedNoise.getOctave(i);
                if (oct) lowerSum += oct->sample(fx, fy, fz, yScaleArg2, yMaxArg2) / amp;
            }
            if (!useLowerOnly) {
                const PerlinNoiseSampler* oct = upperInterpolatedNoise.getOctave(i);
                if (oct) upperSum += oct->sample(fx, fy, fz, yScaleArg2, yMaxArg2) / amp;
            }
            amp /= 2.0;
        }
        return mh_clampedLerp(lowerSum / 512.0, upperSum / 512.0, t) / 128.0;
    }
};

// Bundles everything sampleSlopedCheese needs. continentalness/erosion/weirdness reuse
// WorldNoiseCache's already-validated samplers directly (mathematically identical --
// same deriver-construction sequence, see terrain_shaper.h / density port notes).
struct DensityContext {
    const WorldNoiseCache* noise; // for caveLayer/caveCheese + continentalness/erosion/ridge reuse
    DoublePerlinNoiseSampler shiftNoise;
    DoublePerlinNoiseSampler jaggedNoise;
    InterpolatedNoiseSampler terrainNoise;
    VanillaTerrainParameters terrainParams;

    static DensityContext build(const WorldNoiseCache& worldCache, int64_t seed) {
        DensityContext c;
        c.noise = &worldCache;
        Xoroshiro128PP base = Xoroshiro128PP::fromSingleSeed(seed);
        Xoroshiro128PP deriver = base.createRandomDeriver();
        // [bytecode-verified] shiftNoise = deriver.createNoiseSampler(OFFSET) (firstOctave=-3, [1,1,1,0])
        {
            Xoroshiro128PP r = deriver.createRandom("minecraft:offset");
            c.shiftNoise = DoublePerlinNoiseSampler::create(r, -3, {1.0, 1.0, 1.0, 0.0});
        }
        // [bytecode-verified] jaggedNoise = deriver.createNoiseSampler(JAGGED) (firstOctave=-16, 16x1.0)
        {
            Xoroshiro128PP r = deriver.createRandom("minecraft:jagged");
            c.jaggedNoise = DoublePerlinNoiseSampler::create(r, -16, std::vector<double>(16, 1.0));
        }
        // [bytecode-verified] terrainNoise = new InterpolatedNoiseSampler(deriver.createRandom("minecraft:terrain"), 4, 8)
        c.terrainNoise = InterpolatedNoiseSampler::create(deriver.createRandom("minecraft:terrain"), 4, 8);
        // terrainParams is entirely seed-independent (vanilla constants only) -- built ONCE
        // and shared via the SplinePtrs' refcount, not reconstructed from scratch per seed.
        // Measured cost of not doing this: ~74 heap-allocated Spline nodes x every seed in
        // a batch, both to construct AND (more expensively) to destroy when a batch's
        // std::vector<DensityContext> goes out of scope -- this was the actual dominant
        // per-batch cost in a large multi-seed run, not the H2D transfer it was first
        // mistaken for. C++11 guarantees thread-safe static-local init, so this is safe to
        // call from the parallel per-seed build loop in phase1_scan.cu.
        static const VanillaTerrainParameters cachedTerrainParams = VanillaTerrainParameters::createSurfaceParameters();
        c.terrainParams = cachedTerrainParams;
        return c;
    }

    // [bytecode-verified] sampleShiftNoise(x,y,z) = shiftNoise.sample(x,y,z) * 4.0
    double sampleShiftNoise(double x, double y, double z) const {
        return shiftNoise.sample(x, y, z) * 4.0;
    }

    struct NoiseInfo {
        double shiftedX, shiftedZ;
        double offset, factor, peaks;
    };

    // [bytecode-verified] createNoiseInfo: NOTE the shiftedZ argument order
    // (cellZ, cellX, 0), not the naive (cellZ, 0, cellX).
    NoiseInfo createNoiseInfo(int cellX, int cellZ) const {
        NoiseInfo info;
        info.shiftedX = cellX + sampleShiftNoise(cellX, 0, cellZ);
        info.shiftedZ = cellZ + sampleShiftNoise(cellZ, cellX, 0);
        double continentalness = noise->continentalness.sample(info.shiftedX, 0, info.shiftedZ);
        double weirdness = noise->ridge.sample(info.shiftedX, 0, info.shiftedZ); // ridge === weirdness (proven identical deriver)
        double erosion = noise->erosion.sample(info.shiftedX, 0, info.shiftedZ);
        // [bytecode-verified] createTerrainNoisePoint(continentalness, weirdness, erosion)
        //   -> terrainParameters.createNoisePoint(continentalness, erosion, weirdness) (float-narrowed)
        NoisePointF np = VanillaTerrainParameters::createNoisePoint(
            (float)continentalness, (float)erosion, (float)weirdness);
        info.offset = terrainParams.getOffset(np);
        info.factor = terrainParams.getFactor(np);
        info.peaks = terrainParams.getPeak(np);
        return info;
    }

    // [bytecode-verified] sampleJaggedNoise(peaks, shiftedX, shiftedZ)
    double sampleJaggedNoise(double peaks, double shiftedX, double shiftedZ) const {
        if (peaks == 0.0) return 0.0;
        double j = jaggedNoise.sample(shiftedX * 1500.0, 0, shiftedZ * 1500.0);
        if (j > 0.0) return peaks * j;
        return (peaks / 2.0) * j;
    }

    static double noiseGradientDensity(double factor, double depth) {
        double f = depth * factor;
        return f > 0.0 ? f * 4.0 : f;
    }
    static double clampedMap(double value, double oldMin, double oldMax, double newMin, double newMax) {
        return mh_clampedLerp(newMin, newMax, (value - oldMin) / (oldMax - oldMin));
    }

    // [bytecode-verified via SearchCoords.sampleSlopedCheese, already read from Java source]
    double sampleSlopedCheese(int x, int y, int z) const {
        int cellX = x >> 2;
        int cellZ = z >> 2;
        NoiseInfo info = createNoiseInfo(cellX, cellZ);
        double jagged = sampleJaggedNoise(info.peaks, info.shiftedX, info.shiftedZ);
        double depth = info.offset + clampedMap(y, -64, 320, 1.5, -1.5);
        double density = noiseGradientDensity(info.factor, depth + jagged);
        density += terrainNoise.calculateNoise(cellX, y, cellZ);
        return density;
    }

    // SearchCoords.cheeseWithSloped
    double cheeseWithSloped(int x, int y, int z) const {
        double layer = 4.0 * noise->caveLayer.sample(x, y * 8.0, z) * noise->caveLayer.sample(x, y * 8.0, z);
        double caveCheeseVal = mh_clamp(0.27 + noise->caveCheese.sample(x, y * 0.6666666666666666, z), -1.0, 1.0);
        double sloped = sampleSlopedCheese(x, y, z);
        double slopedTerm = mh_clamp(1.5 + (-0.64 * sloped), 0.0, 0.5);
        return layer + caveCheeseVal + slopedTerm;
    }

    bool isDensityLadderAir(int x, int y, int z) const {
        double cheese = cheeseWithSloped(x, y, z);
        if (y > 50) return Entrance(*noise, x, y, z) < 0.0;
        if (y >= 10) return Entrance(*noise, x, y, z) < 0.0 || cheese < 0.0;
        return Entrance2(*noise, x, y, z) < 0.0 || cheese < 0.0;
    }

    int estimateCheeseSurfaceLadder(int x, int z) const {
        if (!isDensityLadderAir(x, 60, z)) {
            if (!isDensityLadderAir(x, 70, z)) return 70;
            return 63;
        }
        if (!isDensityLadderAir(x, 50, z)) return 60;
        static const int LADDER[10] = {40, 30, 20, 10, 0, -10, -20, -30, -40, -50};
        for (int i = 0; i < 10; i++) {
            if (!isDensityLadderAir(x, LADDER[i], z)) {
                if (i == 0) return 50;
                return LADDER[i - 1];
            }
        }
        return -54;
    }

    // SearchCoords.passesDensityPrefilter (isHutRotated90/mapHutLocalToWorld already
    // exist in swamphut.h / are trivial arithmetic respectively).
    bool passesDensityPrefilter(int64_t seed, int hutX, int hutZ, int maxHeight) const {
        bool rotated = isHutRotated90(seed, hutX, hutZ);
        static const int PROBES[5][2] = {{3, 4}, {3, 1}, {3, 7}, {1, 4}, {5, 4}};
        int sum = 0;
        for (auto& p : PROBES) {
            int wx, wz;
            if (!rotated) { wx = hutX + p[0]; wz = hutZ + p[1]; }
            else { wx = hutX + p[1]; wz = hutZ + p[0]; }
            sum += estimateCheeseSurfaceLadder(wx, wz);
        }
        double mean = sum / 5.0;
        // Margin covers the gap between this 5-probe ladder estimate and the real hut floor.

        // Tunable so it can be MEASURED rather than assumed -- see validate_margin below.

        return !(mean > maxHeight + DENSITY_MARGIN);
    }
};

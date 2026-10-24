#pragma once
#include "xoroshiro128pp.h"
#include <cstdint>
#include <cmath>
#include <vector>
#include <string>

#ifdef __CUDACC__
#define PN_HD __host__ __device__
#else
#define PN_HD
#endif

PN_HD inline double pn_lerp(double t, double a, double b) { return a + t * (b - a); }
PN_HD inline double pn_lerp2(double tx, double ty, double v00, double v10, double v01, double v11) {
    return pn_lerp(ty, pn_lerp(tx, v00, v10), pn_lerp(tx, v01, v11));
}
PN_HD inline double pn_lerp3(double tx, double ty, double tz,
                              double v000, double v100, double v010, double v110,
                              double v001, double v101, double v011, double v111) {
    return pn_lerp(tz, pn_lerp2(tx, ty, v000, v100, v010, v110),
                        pn_lerp2(tx, ty, v001, v101, v011, v111));
}

PN_HD inline double pn_fade(double t) { return t * t * t * (t * (t * 6.0 - 15.0) + 10.0); }

PN_HD inline double pn_maintainPrecision(double v) {
    return v - floor(v / 3.3554432E7 + 0.5) * 3.3554432E7;
}

PN_HD inline double pn_grad(int hash, double x, double y, double z) {
    int h = hash & 15;
    double u = h < 8 ? x : y;
    double v = h < 4 ? y : (h == 12 || h == 14 ? x : z);
    return ((h & 1) == 0 ? u : -u) + ((h & 2) == 0 ? v : -v);
}

struct PerlinNoiseSampler {
    double originX, originY, originZ;
    uint8_t permutations[256];

    PN_HD static PerlinNoiseSampler create(Xoroshiro128PP& source) {
        PerlinNoiseSampler p;
        p.originX = source.nextDouble() * 256.0;
        p.originY = source.nextDouble() * 256.0;
        p.originZ = source.nextDouble() * 256.0;
        for (int i = 0; i < 256; i++) p.permutations[i] = (uint8_t)i;
        for (int i = 0; i < 256; i++) {
            int j = source.nextInt(256 - i);
            uint8_t tmp = p.permutations[i];
            p.permutations[i] = p.permutations[i + j];
            p.permutations[i + j] = tmp;
        }
        return p;
    }

    PN_HD int getGradient(int x) const { return permutations[x & 255] & 255; }

    PN_HD double sample(double x, double y, double z, double yScale, double yMax) const {
        double x2 = x + originX, y2 = y + originY, z2 = z + originZ;
        int ix = (int)floor(x2), iy = (int)floor(y2), iz = (int)floor(z2);
        double fx = x2 - ix, fy = y2 - iy, fz = z2 - iz;
        double fy2;
        if (yScale != 0.0) {
            double clampedY = (yMax >= 0.0 && yMax < fy) ? yMax : fy;
            fy2 = floor(clampedY / yScale + 1.0E-7) * yScale;
        } else {
            fy2 = 0.0;
        }

        return sampleAndLerp(ix, iy, iz, fx, fy - fy2, fz, fy);
    }

    PN_HD double sampleAndLerp(int ix, int iy, int iz, double fx, double fy, double fz, double fyForFade) const {
        int a  = getGradient(ix);
        int a1 = getGradient(ix + 1);
        int b  = getGradient(a + iy);
        int b1 = getGradient(a + iy + 1);
        int c  = getGradient(a1 + iy);
        int c1 = getGradient(a1 + iy + 1);
        double v000 = pn_grad(getGradient(b + iz), fx, fy, fz);
        double v100 = pn_grad(getGradient(c + iz), fx - 1.0, fy, fz);
        double v010 = pn_grad(getGradient(b1 + iz), fx, fy - 1.0, fz);
        double v110 = pn_grad(getGradient(c1 + iz), fx - 1.0, fy - 1.0, fz);
        double v001 = pn_grad(getGradient(b + iz + 1), fx, fy, fz - 1.0);
        double v101 = pn_grad(getGradient(c + iz + 1), fx - 1.0, fy, fz - 1.0);
        double v011 = pn_grad(getGradient(b1 + iz + 1), fx, fy - 1.0, fz - 1.0);
        double v111 = pn_grad(getGradient(c1 + iz + 1), fx - 1.0, fy - 1.0, fz - 1.0);
        double tx = pn_fade(fx), ty = pn_fade(fyForFade), tz = pn_fade(fz);
        return pn_lerp3(tx, ty, tz, v000, v100, v010, v110, v001, v101, v011, v111);
    }
};

struct OctavePerlinNoiseSampler {
    int firstOctave;
    std::vector<double> amplitudes;
    double lacunarity;
    double persistence;
    std::vector<bool> hasOctave;
    std::vector<PerlinNoiseSampler> octaveSamplers;

    static OctavePerlinNoiseSampler create(Xoroshiro128PP& random, int firstOctave, const std::vector<double>& amplitudes) {
        OctavePerlinNoiseSampler o;
        o.firstOctave = firstOctave;
        o.amplitudes = amplitudes;
        int n = (int)amplitudes.size();
        o.lacunarity = pow(2.0, firstOctave);
        o.persistence = pow(2.0, n - 1) / (pow(2.0, n) - 1.0);
        o.hasOctave.assign(n, false);
        o.octaveSamplers.resize(n);

        Xoroshiro128PP octaveDeriver = random.createRandomDeriver();
        for (int i = 0; i < n; i++) {
            if (amplitudes[i] == 0.0) continue;
            int octave = firstOctave + i;

            std::string name = "octave_" + std::to_string(octave);
            Xoroshiro128PP perOctaveRandom = octaveDeriver.createRandom(name);
            o.octaveSamplers[i] = PerlinNoiseSampler::create(perOctaveRandom);
            o.hasOctave[i] = true;
        }
        return o;
    }

    PN_HD double sample(double x, double y, double z) const {
        double result = 0.0;
        double freq = lacunarity;
        double amp = persistence;
        for (size_t i = 0; i < amplitudes.size(); i++) {
            if (!hasOctave[i]) { freq *= 2.0; amp /= 2.0; continue; }
            double sx = pn_maintainPrecision(x * freq);
            double sy = pn_maintainPrecision(y * freq);
            double sz = pn_maintainPrecision(z * freq);
            double value = octaveSamplers[i].sample(sx, sy, sz, 0.0, 0.0);
            result += amplitudes[i] * value * amp;
            freq *= 2.0;
            amp /= 2.0;
        }
        return result;
    }
};

struct DoublePerlinNoiseSampler {
    OctavePerlinNoiseSampler firstSampler;
    OctavePerlinNoiseSampler secondSampler;
    double amplitude;

    static DoublePerlinNoiseSampler create(Xoroshiro128PP random, int firstOctave, const std::vector<double>& amplitudes) {
        DoublePerlinNoiseSampler d;

        d.firstSampler = OctavePerlinNoiseSampler::create(random, firstOctave, amplitudes);
        d.secondSampler = OctavePerlinNoiseSampler::create(random, firstOctave, amplitudes);

        int minOctave = INT32_MAX, maxOctave = INT32_MIN;
        for (int i = 0; i < (int)amplitudes.size(); i++) {
            if (amplitudes[i] == 0.0) continue;
            if (i < minOctave) minOctave = i;
            if (i > maxOctave) maxOctave = i;
        }
        auto createAmplitude = [](int n) { return 0.1 * (1.0 + 1.0 / (double)(n + 1)); };
        d.amplitude = 0.16666666666666666 / createAmplitude(maxOctave - minOctave);
        return d;
    }

    PN_HD double sample(double x, double y, double z) const {
        const double f = 1.0181268882175227;
        double a = firstSampler.sample(x, y, z);
        double b = secondSampler.sample(x * f, y * f, z * f);
        return (a + b) * amplitude;
    }
};

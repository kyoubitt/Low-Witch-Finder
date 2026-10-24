#pragma once
#include <cstdint>
#include <cstdlib>
#include <vector>
namespace ts {
typedef unsigned long long u64; typedef long long i64; typedef unsigned u32;
static const u64 M48 = 0xFFFFFFFFFFFFULL;
static const int RADIUS = 58593;
static const u64 C0 = 14357620ULL, C1 = 341873128712ULL, C2 = 132897987541ULL;
struct Axis { int n; i64 lo[2], hi[2]; };
static inline Axis axisOf(int regime) {
    Axis a; a.n = 1; a.lo[1] = a.hi[1] = 0;
    if (regime == 0)      { a.lo[0] = -16384;  a.hi[0] = 16383; }
    else if (regime == 1) { a.n = 2; a.lo[0] = -32768; a.hi[0] = -16385; a.lo[1] = 16384; a.hi[1] = 32767; }
    else                  { a.n = 2; a.lo[0] = -RADIUS; a.hi[0] = -32769; a.lo[1] = 32768; a.hi[1] = RADIUS; }
    return a;
}
static inline u64 axisCount(const Axis& a) { u64 s = 0; for (int i = 0; i < a.n; i++) s += (u64)(a.hi[i] - a.lo[i] + 1); return s; }
static inline bool axisHas(const Axis& a, i64 r) { for (int i = 0; i < a.n; i++) if (r >= a.lo[i] && r <= a.hi[i]) return true; return false; }
static inline u64 inv64(u64 b) { u64 x = b; for (int i = 0; i < 7; i++) x *= 2ULL - b * x; return x; }
static const u32 C1_20 = (u32)(C1 & 0xFFFFF), C2_20 = (u32)(C2 & 0xFFFFF), C0_20 = (u32)(C0 & 0xFFFFF);
static inline u32 c2inv20() { static const u32 v = (u32)(inv64(C2) & 0xFFFFF); return v; }
static inline u64 cregOf(i64 rx, i64 rz) { return (C0 + (u64)rx * C1 + (u64)rz * C2) & M48; }

static inline u32 permGroup(u32 p) { u32 x = (p * 0x9E3779B1u) & 0xFFFFFu; x ^= x >> 9; x = (x * 0x85EBCA6Bu) & 0xFFFFFu; x ^= x >> 7; return x & 0xFFFFFu; }
struct Region { int rx, rz; u64 Creg; };

static inline void regionsOfGroup(u32 c20, const Axis& ax, const Axis& az, std::vector<Region>& out) {
    out.clear(); const u32 inv = c2inv20();
    for (int i = 0; i < ax.n; i++) for (i64 rx = ax.lo[i]; rx <= ax.hi[i]; rx++) {
        const u32 rem = (c20 - C0_20 - (u32)((u64)rx * C1_20)) & 0xFFFFFu;
        const u32 r = (rem * inv) & 0xFFFFFu; i64 rz;
        if (r <= (u32)RADIUS) rz = r; else if (r >= (1u << 20) - (u32)RADIUS) rz = (i64)r - (1 << 20); else continue;
        if (!axisHas(az, rz)) continue;
        Region g; g.rx = (int)rx; g.rz = (int)rz; g.Creg = cregOf(rx, rz); out.push_back(g);
    }
}

static inline int oddOffsetCount() { int n = 0; for (int a = 0; a < 24; a++) for (int b = 0; b < 24; b++) if (((a ^ b) & 1) != 0) n++; return n; }

static inline void lidDecode(int lid, int& lx, int& lz) { lx = lid >> 3; lz = ((lid & 7) << 1) | (((lid >> 3) & 1) ^ 1); }

static inline int lidOffsets(int lx, int lz, int* ox, int* oz) { int n = 0; for (int a = lx; a < 24; a += 16) for (int b = lz; b < 24; b += 16) { ox[n] = a; oz[n] = b; n++; } return n; }
}

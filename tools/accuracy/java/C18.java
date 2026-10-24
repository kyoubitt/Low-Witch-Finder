import net.minecraft.SharedConstants;
import net.minecraft.core.*;
import net.minecraft.data.BuiltinRegistries;
import net.minecraft.server.Bootstrap;
import net.minecraft.world.level.biome.*;
import net.minecraft.world.level.levelgen.*;
import java.io.*;

/** 1.18.2: the climate sampler lives in the chunk generator. */
public class BiomeTruth {
    public static void main(String[] a) throws Exception {
        SharedConstants.tryDetectVersion();
        Bootstrap.bootStrap();
        MultiNoiseBiomeSource src = MultiNoiseBiomeSource.Preset.OVERWORLD.biomeSource(BuiltinRegistries.BIOME);
        if (a.length > 1) { TD.dumpSource(src, a[1]); return; }
        BufferedReader in = new BufferedReader(new InputStreamReader(System.in));
        long cur = Long.MIN_VALUE; Climate.Sampler cs = null; String l;
        while ((l = in.readLine()) != null) {
            String[] t = l.trim().split(" ");
            long seed = Long.parseLong(t[0]);
            if (seed != cur || cs == null) {
                NoiseBasedChunkGenerator g = new NoiseBasedChunkGenerator(BuiltinRegistries.STRUCTURE_SETS, BuiltinRegistries.NOISE, src, seed, BuiltinRegistries.NOISE_GENERATOR_SETTINGS.getHolderOrThrow(NoiseGeneratorSettings.OVERWORLD));
                cs = g.climateSampler(); cur = seed;
            }
            int x = Integer.parseInt(t[1]), y = Integer.parseInt(t[2]), z = Integer.parseInt(t[3]);
            Holder<Biome> b = src.getNoiseBiome(x >> 2, y >> 2, z >> 2, cs);
            System.out.println("BIOME " + b.unwrapKey().get().location().getPath());
        }
    }
}

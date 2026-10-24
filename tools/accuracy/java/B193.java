import net.minecraft.SharedConstants;
import net.minecraft.core.*;
import net.minecraft.core.registries.Registries;
import net.minecraft.data.registries.VanillaRegistries;
import net.minecraft.server.Bootstrap;
import net.minecraft.world.level.biome.*;
import net.minecraft.world.level.levelgen.*;
import java.io.*;

/** 1.19.3 */
public class BiomeTruth {
    public static void main(String[] a) throws Exception {
        SharedConstants.tryDetectVersion();
        Bootstrap.bootStrap();
        HolderLookup.Provider p = VanillaRegistries.createLookup();
        NoiseGeneratorSettings ngs = p.lookupOrThrow(Registries.NOISE_SETTINGS).getOrThrow(NoiseGeneratorSettings.OVERWORLD).value();
        MultiNoiseBiomeSource src = MultiNoiseBiomeSource.Preset.OVERWORLD.biomeSource(p.lookupOrThrow(Registries.BIOME));
        if (a.length > 1) { TD.dumpSource(src, a[1]); return; }
        BufferedReader in = new BufferedReader(new InputStreamReader(System.in));
        long cur = Long.MIN_VALUE; RandomState rs = null; String l;
        while ((l = in.readLine()) != null) {
            String[] t = l.trim().split(" ");
            long seed = Long.parseLong(t[0]);
            if (seed != cur || rs == null) { rs = RandomState.create(ngs, p.lookupOrThrow(Registries.NOISE), seed); cur = seed; }
            int x = Integer.parseInt(t[1]), y = Integer.parseInt(t[2]), z = Integer.parseInt(t[3]);
            Holder<Biome> b = src.getNoiseBiome(x >> 2, y >> 2, z >> 2, rs.sampler());
            System.out.println("BIOME " + b.unwrapKey().get().location().getPath());
        }
    }
}

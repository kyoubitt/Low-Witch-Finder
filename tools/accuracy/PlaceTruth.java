import net.minecraft.SharedConstants;
import net.minecraft.server.Bootstrap;
import net.minecraft.world.level.ChunkPos;
import net.minecraft.world.level.levelgen.structure.placement.*;
import java.util.*;
public class PlaceTruth {
    public static void main(String[] a) throws Exception {
        SharedConstants.tryDetectVersion(); Bootstrap.bootStrap();
        RandomSpreadStructurePlacement p = new RandomSpreadStructurePlacement(32, 8, RandomSpreadType.LINEAR, 14357620);
        Scanner in = new Scanner(System.in);
        while (in.hasNext()) {
            long seed = in.nextLong(); int rx = in.nextInt(), rz = in.nextInt();
            ChunkPos c = p.getPotentialStructureChunk(seed, rx * 32, rz * 32);
            System.out.println("R " + c.x + " " + c.z);
        }
    }
}

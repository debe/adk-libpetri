package org.libpetri.adk.verify;

import static com.google.common.truth.Truth.assertWithMessage;

import com.microsoft.z3.Context;
import org.junit.jupiter.api.Test;

/**
 * Fails the build when the Z3 JNI natives are missing on a machine that is
 * supposed to have them.
 *
 * <p>Every SMT test in this repo is guarded by {@code @EnabledIf("z3Available")},
 * whose predicate catches {@link UnsatisfiedLinkError} and returns {@code false}.
 * That is deliberate: a contributor without native Z3 installed should still be
 * able to run {@code ./mvnw verify}. But a JUnit skip is not a failure, so on a
 * machine with no natives the whole verification suite disappears and the build
 * still goes green. CI ran that way until the workflow gained its
 * "Install z3 JNI natives" step, and nothing would have told us.
 *
 * <p>This test closes that hole from the other side. It is enabled only when
 * {@code REQUIRE_Z3} is set, which CI does, so:
 *
 * <ul>
 *   <li>locally, without natives, it skips like everything else;</li>
 *   <li>in CI, if the install step breaks or the asset moves, this fails loudly
 *       instead of the suite quietly shrinking.</li>
 * </ul>
 *
 * <p>Keep it in step with the claim in the README: both demo nets are
 * Z3-proved deadlock-free on every {@code mvn verify}. That claim is only
 * true of CI while this test passes there.
 */
class Z3NativeGateTest {

    /** Set in CI. Absent locally, where skipping is the intended behaviour. */
    private static boolean z3Required() {
        String flag = System.getenv("REQUIRE_Z3");
        return flag != null && !flag.isBlank() && !"false".equalsIgnoreCase(flag);
    }

    @Test
    void z3_natives_load_when_the_environment_says_they_must() {
        if (!z3Required()) {
            return; // Not a CI run: the @EnabledIf skips are the intended path.
        }
        Throwable failure = null;
        try (Context ignored = new Context()) {
            // Constructing a Context is what actually dlopens libz3java.so.
        } catch (UnsatisfiedLinkError | NoClassDefFoundError e) {
            failure = e;
        }
        assertWithMessage(
                        "REQUIRE_Z3 is set, so the Z3 JNI natives must load, but they did not. "
                                + "Every @EnabledIf(\"z3Available\") test would have skipped "
                                + "silently and the build would still have passed. Check the "
                                + "'Install z3 JNI natives' step and LD_LIBRARY_PATH.")
                .that(failure)
                .isNull();
    }
}

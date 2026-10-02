package org.libpetri.adk.verify;

import static com.google.common.truth.Truth.assertWithMessage;

import org.junit.jupiter.api.Test;
import org.libpetri.smt.SmtVerifier;

/**
 * Fails the build when the {@code z3} binary is missing on a machine that is
 * supposed to have it.
 *
 * <p>Since libpetri 4.0 the SMT verifier talks to Z3 by running a {@code z3}
 * executable (on {@code PATH}, or named by {@code LIBPETRI_Z3}) rather than
 * through JNI natives. Every SMT test in this repo is guarded by
 * {@code @EnabledIf("z3Available")}, which delegates to
 * {@link SmtVerifier#z3Available()} and returns {@code false} when no binary is
 * found. That is deliberate: a contributor without Z3 installed should still be
 * able to run {@code ./mvnw verify}. But a JUnit skip is not a failure, so on a
 * machine with no Z3 the whole verification suite disappears and the build
 * still goes green.
 *
 * <p>This test closes that hole from the other side. It is enabled only when
 * {@code REQUIRE_Z3} is set, which CI does, so:
 *
 * <ul>
 *   <li>locally, without Z3, it skips like everything else;</li>
 *   <li>in CI, if the install step breaks, this fails loudly instead of the
 *       suite quietly shrinking.</li>
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
    void z3_binary_is_found_when_the_environment_says_it_must_be() {
        if (!z3Required()) {
            return; // Not a CI run: the @EnabledIf skips are the intended path.
        }
        assertWithMessage(
                        "REQUIRE_Z3 is set, so a z3 binary must be available, but none was "
                                + "found. Every @EnabledIf(\"z3Available\") test would have "
                                + "skipped silently and the build would still have passed. "
                                + "Check the 'Install z3' CI step, PATH, and LIBPETRI_Z3.")
                .that(SmtVerifier.z3Available())
                .isTrue();
    }
}

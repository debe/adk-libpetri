package org.libpetri.adk.verify;

import static com.google.common.truth.Truth.assertWithMessage;

import java.util.LinkedHashMap;
import java.util.Map;
import java.util.function.UnaryOperator;
import org.libpetri.core.PetriNet;
import org.libpetri.smt.SmtProperty;
import org.libpetri.smt.SmtVerifier;

/**
 * Proves several properties of one net, one {@code verify()} call each.
 *
 * <p>{@link SmtVerifier#property(SmtProperty)} replaces the property rather
 * than adding one, so a chain of {@code .property(...)} calls checks only the
 * last. Every proof that claims more than one property goes through here (or
 * through {@code SubnetDef.verify} with a {@code VerificationHarness}, which
 * does accumulate).
 *
 * <p>Each property gets a fresh verifier from {@code configure}, so nothing a
 * {@code verify()} call leaves behind can leak into the next one.
 */
public final class SmtProofs {

    private SmtProofs() {}

    /**
     * Asserts that every property in {@code properties} is {@code Proven} on
     * {@code net}, under the verifier settings {@code configure} applies.
     * Keys label the properties in the failure message, which lists every
     * property that did not prove, with its verdict and libpetri's report.
     *
     * <p>{@code isProven()} is the strong form on purpose: libpetri downgrades
     * a verdict it cannot back (an IC3 certificate or a closed state-space
     * enumeration) to {@code Unknown}, and {@code isViolated() == false} alone
     * would accept that, letting a claimed proof rot silently.
     */
    public static void assertEachProven(
            PetriNet net,
            UnaryOperator<SmtVerifier> configure,
            Map<String, SmtProperty> properties) {
        var failures = new LinkedHashMap<String, String>();
        properties.forEach((label, property) -> {
            var result = configure.apply(SmtVerifier.forNet(net)).property(property).verify();
            if (!result.isProven()) {
                failures.put(label, result.verdict() + "\n" + result.report());
            }
        });
        assertWithMessage("every property must be Proven, one verify() each: %s", failures)
                .that(failures)
                .isEmpty();
    }
}

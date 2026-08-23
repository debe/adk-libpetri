package org.libpetri.adk;

import java.lang.annotation.Documented;
import java.lang.annotation.ElementType;
import java.lang.annotation.Retention;
import java.lang.annotation.RetentionPolicy;
import java.lang.annotation.Target;

/**
 * Marks BIDI/live and SSE-streaming API as <b>beta</b>: it may change
 * incompatibly, or be removed, in any release including a 0.x minor.
 *
 * <p>The whole project is 0.x, so a minor may break API anywhere. This
 * annotation marks the surfaces that move fastest even by that standard. The
 * turn-based core ({@code PetriAgent.of}, the stock non-streaming subnets, the
 * registry) is the settled part and changes there are called out in the
 * CHANGELOG.
 */
@Documented
@Retention(RetentionPolicy.CLASS)
// FIELD and RECORD_COMPONENT matter as much as TYPE here: the beta surface
// includes colour constants and the nested Places/Transitions holders, and
// leaving them out made marking one a compile error rather than an omission.
@Target({ElementType.TYPE, ElementType.METHOD, ElementType.FIELD,
        ElementType.RECORD_COMPONENT, ElementType.CONSTRUCTOR})
public @interface Experimental {}

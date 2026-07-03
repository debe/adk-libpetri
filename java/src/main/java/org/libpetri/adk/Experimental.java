package org.libpetri.adk;

import java.lang.annotation.Documented;
import java.lang.annotation.ElementType;
import java.lang.annotation.Retention;
import java.lang.annotation.RetentionPolicy;
import java.lang.annotation.Target;

/**
 * Marks BIDI/live and SSE-streaming API as <b>beta within 1.x</b>: it may change
 * incompatibly or be removed without a major-version bump. The turn-based core
 * ({@code PetriAgent.of}, the stock non-streaming subnets, the registry) is stable.
 */
@Documented
@Retention(RetentionPolicy.CLASS)
@Target({ElementType.TYPE, ElementType.METHOD})
public @interface Experimental {}

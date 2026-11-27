/* SPDX-License-Identifier: LGPL-2.1-or-later */
#ifndef FTE3600_AUTH_PREPROCESS_H
#define FTE3600_AUTH_PREPROCESS_H

#include <glib.h>

G_BEGIN_DECLS

/* Minimum pixel dynamic range (max - min) required before running CLAHE.
 * Flat/blank or near-uniform sensor frames are copied unchanged so background
 * noise is never amplified into synthetic edges. */
#define FTE3600_AUTH_CLAHE_MIN_SPAN 12u

/* Deterministic pure-C 2D Contrast Limited Adaptive Histogram Equalization
 * (CLAHE) on 8-bit grayscale images. Never mutates src; writes a tightly
 * packed width * height image (stride = width) into dst. */
gboolean fte3600_auth_preprocess_clahe (const guint8 *src,
                                        gsize         src_length,
                                        gsize         src_stride,
                                        guint         width,
                                        guint         height,
                                        guint8       *dst);

G_END_DECLS
#endif

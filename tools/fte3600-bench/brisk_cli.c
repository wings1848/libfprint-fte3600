/* SPDX-License-Identifier: LGPL-2.1-or-later
 * Standalone offline BRISK benchmark CLI for FTE3600 64x80 PGM pairs.
 * Links directly against sam7-brisk (brisk.c + fte3600-brisk.c) with an
 * inline Fte3600MatchProfile shim and optional 4x4 2D CLAHE preprocessing.
 */

#include "fte3600-brisk.h"

#include <ctype.h>
#include <errno.h>
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* Inline Fte3600MatchProfile shim (0x9369 64x80 primary + FTE3600 geometries). */
static const Fte3600MatchProfile bench_profiles[] = {
  { FTE3600_SENSOR_FT9369, 0x9369, 64, 80, 1 },
  { FTE3600_SENSOR_FT9361, 0x9361, 64, 80, 1 },
  { FTE3600_SENSOR_FT9368, 0x9368, 64, 80, 1 },
  { FTE3600_SENSOR_FT9365, 0x9365, 64, 80, 1 },
  { FTE3600_SENSOR_FT9338, 0x9338, 88, 88, 1 },
  { FTE3600_SENSOR_FT9348, 0x9348, 96, 96, 1 },
  { FTE3600_SENSOR_FT9536, 0x9536, 64, 128, 1 },
  { FTE3600_SENSOR_FT9769, 0x9769, 40, 196, 1 },
};

const Fte3600MatchProfile *
fpi_fte3600_match_profile_get (Fte3600Sensor sensor)
{
  for (guint i = 0; i < G_N_ELEMENTS (bench_profiles); i++)
    if (bench_profiles[i].sensor == sensor)
      return &bench_profiles[i];
  return NULL;
}

const Fte3600MatchProfile *
fpi_fte3600_match_profile_find (guint16 model)
{
  for (guint i = 0; i < G_N_ELEMENTS (bench_profiles); i++)
    if (bench_profiles[i].model == model)
      return &bench_profiles[i];
  return NULL;
}

const Fte3600MatchProfile *
fpi_fte3600_match_profile_resolve (const Fte3600MatchProfile *profile)
{
  const Fte3600MatchProfile *canonical;

  if (!profile)
    return NULL;
  canonical = fpi_fte3600_match_profile_get (profile->sensor);
  if (!canonical || canonical->model != profile->model ||
      canonical->width != profile->width || canonical->height != profile->height ||
      canonical->processing_version != profile->processing_version)
    return NULL;
  return canonical;
}

static const Fte3600MatchProfile *
profile_for_dimensions (guint width, guint height)
{
  for (guint i = 0; i < G_N_ELEMENTS (bench_profiles); i++)
    if (bench_profiles[i].width == width && bench_profiles[i].height == height)
      return &bench_profiles[i];
  return NULL;
}

/* Clean 4x4 tile 2D CLAHE with flat-frame guard. */
static void
apply_clahe_4x4 (const guint8 *src, guint8 *dst, guint width, guint height,
                 gdouble clip_factor)
{
  const gsize n_pixels = (gsize) width * height;
  guint8 min_v = 255;
  guint8 max_v = 0;

  for (gsize i = 0; i < n_pixels; i++)
    {
      if (src[i] < min_v)
        min_v = src[i];
      if (src[i] > max_v)
        max_v = src[i];
    }

  /* Flat-frame guard: avoid amplifying sensor noise floor on blank frames. */
  if ((guint) max_v - (guint) min_v < 8u || width < 4u || height < 4u)
    {
      memcpy (dst, src, n_pixels);
      return;
    }

  guint8 lut[4][4][256];

  for (guint ty = 0; ty < 4u; ty++)
    {
      const guint y0 = (ty * height) / 4u;
      const guint y1 = ((ty + 1u) * height) / 4u;

      for (guint tx = 0; tx < 4u; tx++)
        {
          const guint x0 = (tx * width) / 4u;
          const guint x1 = ((tx + 1u) * width) / 4u;
          const guint tile_pixels = (y1 - y0) * (x1 - x0);
          guint hist[256] = { 0 };

          for (guint y = y0; y < y1; y++)
            for (guint x = x0; x < x1; x++)
              hist[src[(gsize) y * width + x]]++;

          guint clip_limit = (guint) lround (clip_factor * (gdouble) tile_pixels / 256.0);
          if (clip_limit < 2u)
            clip_limit = 2u;

          guint excess = 0;
          for (guint b = 0; b < 256u; b++)
            {
              if (hist[b] > clip_limit)
                {
                  excess += hist[b] - clip_limit;
                  hist[b] = clip_limit;
                }
            }

          const guint add_each = excess / 256u;
          const guint rem = excess % 256u;
          for (guint b = 0; b < 256u; b++)
            hist[b] += add_each;

          if (rem > 0u)
            {
              const guint step = MAX (1u, 256u / rem);
              guint distributed = 0;
              for (guint b = 0; b < 256u && distributed < rem; b += step)
                {
                  hist[b]++;
                  distributed++;
                }
              for (guint b = 0; b < 256u && distributed < rem; b++)
                {
                  hist[b]++;
                  distributed++;
                }
            }

          guint cdf = 0;
          guint cdf_min = 0;
          gboolean have_min = FALSE;
          guint cdf_arr[256];

          for (guint b = 0; b < 256u; b++)
            {
              cdf += hist[b];
              cdf_arr[b] = cdf;
              if (!have_min && cdf > 0u)
                {
                  cdf_min = cdf;
                  have_min = TRUE;
                }
            }

          if (!have_min || tile_pixels <= cdf_min)
            {
              for (guint b = 0; b < 256u; b++)
                lut[ty][tx][b] = (guint8) b;
            }
          else
            {
              const gdouble denom = (gdouble) (tile_pixels - cdf_min);
              for (guint b = 0; b < 256u; b++)
                {
                  if (cdf_arr[b] <= cdf_min)
                    {
                      lut[ty][tx][b] = 0u;
                    }
                  else
                    {
                      long mapped = lround (255.0 * (gdouble) (cdf_arr[b] - cdf_min) / denom);
                      lut[ty][tx][b] = (guint8) CLAMP (mapped, 0L, 255L);
                    }
                }
            }
        }
    }

  for (guint y = 0; y < height; y++)
    {
      const gdouble fy = CLAMP (((gdouble) y + 0.5) * 4.0 / (gdouble) height - 0.5, 0.0, 3.0);
      const guint ty0 = (guint) floor (fy);
      const guint ty1 = MIN (ty0 + 1u, 3u);
      const gdouble wy = fy - (gdouble) ty0;

      for (guint x = 0; x < width; x++)
        {
          const gdouble fx = CLAMP (((gdouble) x + 0.5) * 4.0 / (gdouble) width - 0.5, 0.0, 3.0);
          const guint tx0 = (guint) floor (fx);
          const guint tx1 = MIN (tx0 + 1u, 3u);
          const gdouble wx = fx - (gdouble) tx0;

          const guint8 v = src[(gsize) y * width + x];
          const gdouble top = (1.0 - wx) * (gdouble) lut[ty0][tx0][v] +
                              wx * (gdouble) lut[ty0][tx1][v];
          const gdouble bot = (1.0 - wx) * (gdouble) lut[ty1][tx0][v] +
                              wx * (gdouble) lut[ty1][tx1][v];
          const long val = lround ((1.0 - wy) * top + wy * bot);
          dst[(gsize) y * width + x] = (guint8) CLAMP (val, 0L, 255L);
        }
    }
}

static gboolean
read_pgm_token (const guint8 *buf, gsize len, gsize *pos, char *out, gsize out_size)
{
  gsize i = *pos;

  while (i < len)
    {
      if (isspace (buf[i]))
        {
          i++;
          continue;
        }
      if (buf[i] == '#')
        {
          while (i < len && buf[i] != '\n')
            i++;
          continue;
        }
      break;
    }

  if (i >= len)
    return FALSE;

  gsize k = 0;
  while (i < len && !isspace (buf[i]) && buf[i] != '#')
    {
      if (k + 1 >= out_size)
        return FALSE;
      out[k++] = (char) buf[i++];
    }
  out[k] = '\0';
  *pos = i;
  return k > 0;
}

static gboolean
load_pgm (const char *path, guint8 **pixels_out, guint *width_out, guint *height_out)
{
  g_autofree gchar *raw = NULL;
  gsize len = 0;
  char tok[32];
  gsize pos = 0;

  if (!g_file_get_contents (path, &raw, &len, NULL))
    return FALSE;

  const guint8 *buf = (const guint8 *) raw;
  if (!read_pgm_token (buf, len, &pos, tok, sizeof (tok)) || strcmp (tok, "P5") != 0)
    return FALSE;

  if (!read_pgm_token (buf, len, &pos, tok, sizeof (tok)))
    return FALSE;
  char *endptr = NULL;
  unsigned long w = strtoul (tok, &endptr, 10);
  if (*endptr != '\0' || w < FPI_BRISK_MIN_IMAGE_DIMENSION || w > FPI_BRISK_MAX_IMAGE_DIMENSION)
    return FALSE;

  if (!read_pgm_token (buf, len, &pos, tok, sizeof (tok)))
    return FALSE;
  unsigned long h = strtoul (tok, &endptr, 10);
  if (*endptr != '\0' || h < FPI_BRISK_MIN_IMAGE_DIMENSION || h > FPI_BRISK_MAX_IMAGE_DIMENSION)
    return FALSE;

  if (!read_pgm_token (buf, len, &pos, tok, sizeof (tok)))
    return FALSE;
  unsigned long maxval = strtoul (tok, &endptr, 10);
  if (*endptr != '\0' || maxval != 255ul)
    return FALSE;

  if (pos >= len || !isspace (buf[pos]))
    return FALSE;
  pos++;

  const gsize n_pixels = (gsize) w * (gsize) h;
  if (len - pos < n_pixels)
    return FALSE;

  *pixels_out = g_memdup2 (buf + pos, n_pixels);
  *width_out = (guint) w;
  *height_out = (guint) h;
  return TRUE;
}

typedef struct
{
  guint                     width;
  guint                     height;
  const Fte3600MatchProfile *profile;
  Fte3600BriskStatus        extract_status;
  gboolean                  self_ok;
  gdouble                   extract_ms;
  Fte3600BriskFeatureSet    features;
} CachedFrame;

static CachedFrame *
extract_frame (const char *path, gboolean use_clahe)
{
  g_autofree guint8 *pixels = NULL;
  g_autofree guint8 *clahe_buf = NULL;
  guint width = 0;
  guint height = 0;

  if (!load_pgm (path, &pixels, &width, &height))
    return NULL;

  const Fte3600MatchProfile *profile = profile_for_dimensions (width, height);
  if (!profile)
    return NULL;

  CachedFrame *frame = g_new0 (CachedFrame, 1);
  frame->width = width;
  frame->height = height;
  frame->profile = profile;

  const guint8 *input_pixels = pixels;
  const gint64 t0 = g_get_monotonic_time ();
  if (use_clahe)
    {
      clahe_buf = g_malloc ((gsize) width * height);
      apply_clahe_4x4 (pixels, clahe_buf, width, height, 4.0);
      input_pixels = clahe_buf;
    }

  const FpiBriskImage img = {
    input_pixels,
    (gsize) width * height,
    width,
    height,
    width
  };
  frame->extract_status = fpi_fte3600_brisk_extract_for_profile (profile, &img, &frame->features);
  const gint64 t1 = g_get_monotonic_time ();
  frame->extract_ms = (gdouble) (t1 - t0) / 1000.0;

  if (frame->extract_status == FTE3600_BRISK_OK)
    {
      Fte3600BriskMatchResult self_res = { 0 };
      const Fte3600BriskStatus self_st = fpi_fte3600_brisk_match_for_profile (
        profile, &frame->features, &frame->features, &self_res);
      frame->self_ok = (self_st == FTE3600_BRISK_OK && self_res.authentication_accepted);
    }

  return frame;
}

static void
print_json_string (const char *s)
{
  putchar ('"');
  for (const unsigned char *p = (const unsigned char *) s; *p; p++)
    {
      if (*p == '"' || *p == '\\')
        {
          putchar ('\\');
          putchar (*p);
        }
      else if (*p < 0x20)
        {
          printf ("\\u%04x", *p);
        }
      else
        {
          putchar (*p);
        }
    }
  putchar ('"');
}

static void
evaluate_and_print (const char *path_a, const CachedFrame *fa,
                    const char *path_b, const CachedFrame *fb,
                    gboolean use_tsv, gboolean include_extract_in_elapsed)
{
  Fte3600BriskMatchResult res = { 0 };
  Fte3600BriskStatus match_st = FTE3600_BRISK_INSUFFICIENT_FEATURES;
  gdouble match_ms = 0.0;

  if (fa->extract_status == FTE3600_BRISK_OK &&
      fb->extract_status == FTE3600_BRISK_OK &&
      fa->profile == fb->profile)
    {
      const gint64 t0 = g_get_monotonic_time ();
      match_st = fpi_fte3600_brisk_match_for_profile (
        fa->profile, &fa->features, &fb->features, &res);
      const gint64 t1 = g_get_monotonic_time ();
      match_ms = (gdouble) (t1 - t0) / 1000.0;
    }

  const gboolean accepted = (match_st == FTE3600_BRISK_OK && res.authentication_accepted);
  const gdouble elapsed_ms = include_extract_in_elapsed
                               ? (fa->extract_ms + fb->extract_ms + match_ms)
                               : match_ms;
  const gdouble inlier_ratio = isfinite (res.inlier_ratio) ? res.inlier_ratio : 0.0;
  const gdouble rms_error = isfinite (res.rms_error) ? res.rms_error : 0.0;
  const gdouble mean_hamming = isfinite (res.mean_hamming) ? res.mean_hamming : 0.0;

  if (use_tsv)
    {
      printf ("%s\t%s\t%d\t%u\t%u\t%.6f\t%.6f\t%.6f\t%.6f\t%u\t%u\t%d\t%d\t%d\t%.6f\t%.6f\n",
              path_a, path_b,
              accepted ? 1 : 0,
              res.inliers,
              res.mutual_matches,
              inlier_ratio,
              rms_error,
              mean_hamming,
              elapsed_ms,
              fa->features.n_features,
              fb->features.n_features,
              (int) fa->extract_status,
              (int) fb->extract_status,
              (fa->self_ok && fb->self_ok) ? 1 : 0,
              fa->extract_ms,
              fb->extract_ms);
      return;
    }

  printf ("{\"a\":");
  print_json_string (path_a);
  printf (",\"b\":");
  print_json_string (path_b);
  printf (",\"accepted\":%s"
          ",\"diagnostic_passed\":%s"
          ",\"inliers\":%u"
          ",\"mutual_matches\":%u"
          ",\"competing_inliers\":%u"
          ",\"inlier_ratio\":%.6f"
          ",\"rms_error\":%.6f"
          ",\"median_error\":%.6f"
          ",\"mean_hamming\":%.6f"
          ",\"elapsed_ms\":%.6f"
          ",\"match_ms\":%.6f"
          ",\"extract_a_ms\":%.6f"
          ",\"extract_b_ms\":%.6f"
          ",\"features_a\":%u"
          ",\"features_b\":%u"
          ",\"extract_status_a\":%d"
          ",\"extract_status_b\":%d"
          ",\"self_ok_a\":%s"
          ",\"self_ok_b\":%s"
          ",\"match_status\":%d"
          "}\n",
          accepted ? "true" : "false",
          res.diagnostic_policy_passed ? "true" : "false",
          res.inliers,
          res.mutual_matches,
          res.competing_inliers,
          inlier_ratio,
          rms_error,
          isfinite (res.median_error) ? res.median_error : 0.0,
          mean_hamming,
          elapsed_ms,
          match_ms,
          fa->extract_ms,
          fb->extract_ms,
          fa->features.n_features,
          fb->features.n_features,
          (int) fa->extract_status,
          (int) fb->extract_status,
          fa->self_ok ? "true" : "false",
          fb->self_ok ? "true" : "false",
          (int) match_st);
}

static void
usage (const char *prog)
{
  fprintf (stderr,
           "Usage:\n"
           "  %s [--clahe] [--json|--tsv] <img_a.pgm> <img_b.pgm>\n"
           "  %s [--clahe] [--json|--tsv] --batch   (reads 'path_a\\tpath_b' lines from stdin)\n",
           prog, prog);
}

int
main (int argc, char **argv)
{
  gboolean use_clahe = FALSE;
  gboolean use_tsv = FALSE;
  gboolean batch = FALSE;
  const char *pos_args[2] = { NULL, NULL };
  int n_pos = 0;

  for (int i = 1; i < argc; i++)
    {
      if (strcmp (argv[i], "--clahe") == 0)
        {
          use_clahe = TRUE;
        }
      else if (strcmp (argv[i], "--tsv") == 0)
        {
          use_tsv = TRUE;
        }
      else if (strcmp (argv[i], "--json") == 0)
        {
          use_tsv = FALSE;
        }
      else if (strcmp (argv[i], "--batch") == 0)
        {
          batch = TRUE;
        }
      else if (strcmp (argv[i], "-h") == 0 || strcmp (argv[i], "--help") == 0)
        {
          usage (argv[0]);
          return 0;
        }
      else if (argv[i][0] == '-')
        {
          fprintf (stderr, "Unknown option: %s\n", argv[i]);
          usage (argv[0]);
          return 2;
        }
      else if (n_pos < 2)
        {
          pos_args[n_pos++] = argv[i];
        }
      else
        {
          usage (argv[0]);
          return 2;
        }
    }

  if (batch && n_pos != 0)
    {
      usage (argv[0]);
      return 2;
    }
  if (!batch && n_pos != 2)
    {
      usage (argv[0]);
      return 2;
    }

  if (use_tsv)
    {
      printf ("a\tb\taccepted\tinliers\tmutual_matches\tinlier_ratio\trms_error\t"
              "mean_hamming\telapsed_ms\tfeatures_a\tfeatures_b\t"
              "extract_status_a\textract_status_b\tself_ok_both\textract_a_ms\textract_b_ms\n");
    }

  if (!batch)
    {
      g_autofree CachedFrame *fa = extract_frame (pos_args[0], use_clahe);
      g_autofree CachedFrame *fb = extract_frame (pos_args[1], use_clahe);
      if (!fa || !fb)
        {
          fprintf (stderr, "Failed to load valid 64x80 (or supported FTE3600) P5 PGM pair\n");
          return 1;
        }
      evaluate_and_print (pos_args[0], fa, pos_args[1], fb, use_tsv, TRUE);
      return 0;
    }

  GHashTable *cache = g_hash_table_new_full (g_str_hash, g_str_equal, g_free, g_free);
  char line[8192];

  while (fgets (line, sizeof (line), stdin) != NULL)
    {
      size_t len = strlen (line);
      while (len > 0 && (line[len - 1] == '\n' || line[len - 1] == '\r'))
        line[--len] = '\0';
      if (len == 0)
        continue;

      char *sep = strchr (line, '\t');
      if (!sep)
        sep = strchr (line, ' ');
      if (!sep)
        {
          fprintf (stderr, "Invalid batch line (expected two paths): %s\n", line);
          g_hash_table_destroy (cache);
          return 1;
        }
      *sep = '\0';
      const char *path_a = line;
      const char *path_b = sep + 1;
      while (*path_b == ' ' || *path_b == '\t')
        path_b++;

      CachedFrame *fa = g_hash_table_lookup (cache, path_a);
      if (!fa)
        {
          fa = extract_frame (path_a, use_clahe);
          if (!fa)
            {
              fprintf (stderr, "Failed to load PGM: %s\n", path_a);
              g_hash_table_destroy (cache);
              return 1;
            }
          g_hash_table_insert (cache, g_strdup (path_a), fa);
        }

      CachedFrame *fb = g_hash_table_lookup (cache, path_b);
      if (!fb)
        {
          fb = extract_frame (path_b, use_clahe);
          if (!fb)
            {
              fprintf (stderr, "Failed to load PGM: %s\n", path_b);
              g_hash_table_destroy (cache);
              return 1;
            }
          g_hash_table_insert (cache, g_strdup (path_b), fb);
        }

      evaluate_and_print (path_a, fa, path_b, fb, use_tsv, FALSE);
    }

  g_hash_table_destroy (cache);
  return 0;
}

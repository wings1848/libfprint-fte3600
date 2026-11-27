/* SPDX-License-Identifier: LGPL-2.1-or-later
 * Deterministic pure-C 2D CLAHE preprocessing for FTE3600 grayscale frames.
 */
#include "fte3600_auth_preprocess.h"

#include <string.h>

#define CLAHE_MAX_TILES 4u

static guint
choose_tiles (guint extent)
{
  if (extent < 32u)
    return MAX (1u, extent / 16u);
  if (extent <= 40u)
    return 2u;
  return CLAHE_MAX_TILES;
}

static gboolean
valid_layout (const guint8 *src, gsize src_length, gsize src_stride,
              guint width, guint height, const guint8 *dst)
{
  if (!src || !dst || width == 0u || height == 0u || src_stride < width)
    return FALSE;
  if (height - 1u > (G_MAXSIZE - width) / src_stride)
    return FALSE;
  return src_length >= (gsize) (height - 1u) * src_stride + width;
}

static void
build_tile_lut (const guint8 *src, gsize src_stride,
                guint x0, guint y0, guint x1, guint y1,
                guint8 lut[256])
{
  const guint32 tile_pixels = (guint32) (x1 - x0) * (guint32) (y1 - y0);
  guint32 hist[256] = { 0 };

  for (guint y = y0; y < y1; y++)
    {
      const guint8 *row = src + (gsize) y * src_stride;
      for (guint x = x0; x < x1; x++)
        hist[row[x]]++;
    }

  /* Clip limit ~2.2x average bin count per tile (tile_pixels / 256). */
  const guint32 clip_limit = MAX (1u, (22u * tile_pixels + 1280u) / 2560u);
  guint32 excess = 0;
  for (guint b = 0; b < 256u; b++)
    {
      if (hist[b] > clip_limit)
        {
          excess += hist[b] - clip_limit;
          hist[b] = clip_limit;
        }
    }

  /* Redistribute clipped excess uniformly across bins below clip_limit. */
  while (excess > 0u)
    {
      guint32 avail = 0;
      for (guint b = 0; b < 256u; b++)
        if (hist[b] < clip_limit)
          avail++;
      if (avail == 0u)
        break;
      const guint32 inc = excess / avail;
      if (inc > 0u)
        {
          for (guint b = 0; b < 256u; b++)
            {
              if (hist[b] < clip_limit)
                {
                  const guint32 add = MIN (inc, clip_limit - hist[b]);
                  hist[b] += add;
                  excess -= add;
                }
            }
        }
      else
        {
          const guint32 target = excess;
          guint32 acc = avail / 2u;
          for (guint b = 0; b < 256u && excess > 0u; b++)
            {
              if (hist[b] < clip_limit)
                {
                  acc += target;
                  if (acc >= avail)
                    {
                      acc -= avail;
                      hist[b]++;
                      excess--;
                    }
                }
            }
        }
    }

  guint32 cdf = 0;
  for (guint b = 0; b < 256u; b++)
    {
      cdf += hist[b];
      lut[b] = (guint8) ((cdf * 255u + tile_pixels / 2u) / tile_pixels);
    }
}

gboolean
fte3600_auth_preprocess_clahe (const guint8 *src, gsize src_length, gsize src_stride,
                               guint width, guint height, guint8 *dst)
{
  if (!valid_layout (src, src_length, src_stride, width, height, dst))
    return FALSE;

  guint8 min_val = 255u;
  guint8 max_val = 0u;
  for (guint y = 0; y < height; y++)
    {
      const guint8 *row = src + (gsize) y * src_stride;
      for (guint x = 0; x < width; x++)
        {
          const guint8 v = row[x];
          if (v < min_val)
            min_val = v;
          if (v > max_val)
            max_val = v;
        }
    }

  /* Flat/blank-frame guard: never amplify empty/no-finger frames. */
  if ((guint) (max_val - min_val) < FTE3600_AUTH_CLAHE_MIN_SPAN)
    {
      for (guint y = 0; y < height; y++)
        memcpy (dst + (gsize) y * width, src + (gsize) y * src_stride, width);
      return TRUE;
    }

  const guint tiles_x = choose_tiles (width);
  const guint tiles_y = choose_tiles (height);
  guint x_bounds[CLAHE_MAX_TILES + 1u];
  guint y_bounds[CLAHE_MAX_TILES + 1u];
  guint cx2[CLAHE_MAX_TILES];
  guint cy2[CLAHE_MAX_TILES];
  guint8 luts[CLAHE_MAX_TILES][CLAHE_MAX_TILES][256];

  for (guint tx = 0; tx <= tiles_x; tx++)
    x_bounds[tx] = (tx * width) / tiles_x;
  for (guint ty = 0; ty <= tiles_y; ty++)
    y_bounds[ty] = (ty * height) / tiles_y;
  for (guint tx = 0; tx < tiles_x; tx++)
    cx2[tx] = x_bounds[tx] + x_bounds[tx + 1u] - 1u;
  for (guint ty = 0; ty < tiles_y; ty++)
    cy2[ty] = y_bounds[ty] + y_bounds[ty + 1u] - 1u;

  for (guint ty = 0; ty < tiles_y; ty++)
    for (guint tx = 0; tx < tiles_x; tx++)
      build_tile_lut (src, src_stride,
                      x_bounds[tx], y_bounds[ty],
                      x_bounds[tx + 1u], y_bounds[ty + 1u],
                      luts[ty][tx]);

  for (guint y = 0; y < height; y++)
    {
      const guint py2 = 2u * y;
      guint ty0, ty1, wy, dy;

      if (tiles_y == 1u || py2 <= cy2[0])
        {
          ty0 = ty1 = 0u;
          wy = 0u;
          dy = 1u;
        }
      else if (py2 >= cy2[tiles_y - 1u])
        {
          ty0 = ty1 = tiles_y - 1u;
          wy = 0u;
          dy = 1u;
        }
      else
        {
          ty0 = 0u;
          while (ty0 + 1u < tiles_y - 1u && py2 > cy2[ty0 + 1u])
            ty0++;
          ty1 = ty0 + 1u;
          wy = py2 - cy2[ty0];
          dy = cy2[ty1] - cy2[ty0];
        }

      const guint8 *src_row = src + (gsize) y * src_stride;
      guint8 *dst_row = dst + (gsize) y * width;
      guint tx_cur = 0u;
      for (guint x = 0; x < width; x++)
        {
          const guint px2 = 2u * x;
          guint tx0, tx1, wx, dx;

          if (tiles_x == 1u || px2 <= cx2[0])
            {
              tx0 = tx1 = 0u;
              wx = 0u;
              dx = 1u;
            }
          else if (px2 >= cx2[tiles_x - 1u])
            {
              tx0 = tx1 = tiles_x - 1u;
              wx = 0u;
              dx = 1u;
            }
          else
            {
              while (tx_cur + 1u < tiles_x - 1u && px2 > cx2[tx_cur + 1u])
                tx_cur++;
              tx0 = tx_cur;
              tx1 = tx0 + 1u;
              wx = px2 - cx2[tx0];
              dx = cx2[tx1] - cx2[tx0];
            }

          const guint8 p = src_row[x];
          const guint64 v00 = luts[ty0][tx0][p];
          const guint64 v01 = luts[ty0][tx1][p];
          const guint64 v10 = luts[ty1][tx0][p];
          const guint64 v11 = luts[ty1][tx1][p];
          const guint64 top = (dx - wx) * v00 + wx * v01;
          const guint64 bot = (dx - wx) * v10 + wx * v11;
          const guint64 num = (dy - wy) * top + wy * bot;
          const guint64 den = (guint64) dx * (guint64) dy;
          dst_row[x] = (guint8) ((num + den / 2u) / den);
        }
    }

  return TRUE;
}

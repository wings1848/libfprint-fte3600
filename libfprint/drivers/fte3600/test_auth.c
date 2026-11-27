/* SPDX-License-Identifier: LGPL-2.1-or-later */
#include "fte3600_auth.h"
#include "fte3600_auth_preprocess.h"
#include "auth_test_image.h"
#include "sam7-brisk/libfprint/matchers/brisk/brisk.h"
#include <glib/gstdio.h>
#include <string.h>

typedef struct { enum fte3600_factory_model backend;
                 guint16 chip, width, height;
} TestIdentity;
static const TestIdentity identities[] = {
  {FTE3600_MODEL_FT9338, 0x9338, 88, 88}, {FTE3600_MODEL_FT9348, 0x9348, 96, 96},
  {FTE3600_MODEL_FT9361, 0x9361, 64, 80}, {FTE3600_MODEL_FT9536, 0x9536, 64, 128},
  {FTE3600_MODEL_FT9368, 0x9368, 64, 80}, {FTE3600_MODEL_FW9369, 0x9369, 64, 80},
  {FTE3600_MODEL_FT9365, 0x9349, 80, 128}, {FTE3600_MODEL_FT9365, 0x9363, 64, 80},
  {FTE3600_MODEL_FT9365, 0x9365, 64, 80}, {FTE3600_MODEL_FT9365, 0x9372, 56, 56},
  {FTE3600_MODEL_FT9769, 0x9391, 40, 196}, {FTE3600_MODEL_FT9769, 0x9392, 40, 196},
  {FTE3600_MODEL_FT9769, 0x9395, 36, 176}, {FTE3600_MODEL_FT9769, 0x9396, 36, 96},
  {FTE3600_MODEL_FT9769, 0x9397, 32, 176}, {FTE3600_MODEL_FT9769, 0x9398, 36, 144},
};

static Fte3600AuthIdentity
identity_for (const TestIdentity *test)
{
  const struct fte3600_device_status status = {
    .state = FTE3600_DEVICE_IDLE, .error = FTE3600_DEVICE_OK, .model = test->backend,
    .chip_id = test->chip, .width = test->width, .height = test->height,
    .startup_complete = 1, .calibration_complete = 1
  };
  Fte3600AuthIdentity identity;

  g_assert_true (fte3600_auth_identity_from_status (&status, &identity, NULL));
  return identity;
}

static Fte3600AuthTemplate *
enroll (const Fte3600AuthIdentity *identity, guint32 seed)
{
  g_autoptr(GError) error = NULL;
  g_autoptr(Fte3600AuthEnrollment) enrollment = fte3600_auth_enrollment_new (identity, &error);
  const gsize size = identity->width * identity->height;
  g_autofree guint8 *image = g_malloc (size);
  g_assert_no_error (error);
  for (guint i = 0; i < FTE3600_AUTH_ENROLL_SAMPLES; i++)
    {
      guint count = 999;
      fte3600_auth_test_image (image, identity->width, identity->height, seed, i);
      gboolean ok = fte3600_auth_enrollment_add (enrollment, image, size, identity->width, &count, &error);
      if (!ok)
        g_test_message ("chip %04x sample %u: %s", identity->chip_id, i, error->message);
      g_assert_no_error (error);
      g_assert_true (ok);
      g_assert_cmpuint (count, ==, i + 1);
    }
  Fte3600AuthTemplate *print = fte3600_auth_enrollment_finish (enrollment, &error);
  g_assert_no_error (error);
  g_assert_nonnull (print);
  return print;
}

static void
test_profile (gconstpointer data)
{
  const TestIdentity *test = data;
  Fte3600AuthIdentity identity = identity_for (test);

  g_autoptr(GError) error = NULL;
  const gsize size = identity.width * identity.height;
  g_autofree guint8 *image = g_malloc (size);
  g_autoptr(Fte3600AuthTemplate) print = enroll (&identity, FTE3600_AUTH_TEST_SEED);
  g_autoptr(GBytes) wire = fte3600_auth_template_encode (print);
  g_assert_nonnull (wire);
  g_assert_cmpuint (g_bytes_get_size (wire), <=, FTE3600_AUTH_MAX_TEMPLATE_BYTES);
  g_autoptr(Fte3600AuthTemplate) loaded = fte3600_auth_template_decode (wire, &error);
  g_assert_no_error (error);
  g_assert_nonnull (loaded);
  g_autoptr(GBytes) again = fte3600_auth_template_encode (loaded);
  g_assert_true (g_bytes_equal (wire, again));
  g_assert_true (fte3600_auth_identity_equal (&identity, fte3600_auth_template_identity (loaded)));

  /* Stored first features must equal a direct CLAHE + single normalization
   * extraction of native pixels, field by field, independent of struct layout. */
  fte3600_auth_test_image (image, identity.width, identity.height, FTE3600_AUTH_TEST_SEED, 0);
  g_autofree guint8 *preprocessed = g_malloc (size);
  g_assert_true (fte3600_auth_preprocess_clahe (image, size, identity.width,
                                                identity.width, identity.height, preprocessed));
  FpiBriskImage original = {preprocessed, size, identity.width, identity.height, identity.width};
  FpiBriskFeatureSet expected;
  g_assert_cmpint (fpi_brisk_extract (&original, &expected), ==, FPI_BRISK_OK);
  const guint8 *encoded = g_bytes_get_data (wire, NULL);
  g_assert_cmpuint (((guint) encoded[40] << 8) | encoded[41], ==, expected.n_features);
  for (guint i = 0; i < expected.n_features; i++)
    {
      const gfloat fields[3] = {expected.features[i].x, expected.features[i].y, expected.features[i].orientation};
      const guint8 *record = encoded + 44 + i * 44;
      for (guint j = 0; j < 3; j++)
        {
          guint32 value;
          memcpy (&value, &fields[j], sizeof (value));
          const guint8 *p = record + j * 4;
          g_assert_cmpuint (((guint32) p[0] << 24) | ((guint32) p[1] << 16) |
                            ((guint32) p[2] << 8) | p[3], ==, value);
        }
      g_assert_cmpmem (record + 12, 32, expected.features[i].descriptor, 32);
    }

  fte3600_auth_test_image (image, identity.width, identity.height, FTE3600_AUTH_TEST_SEED, 12);
  g_autoptr(Fte3600AuthProbe) probe = fte3600_auth_probe_new (&identity, image, size, identity.width, &error);
  g_assert_no_error (error);
  Fte3600AuthMatch result;
  g_autoptr(GBytes) query_bytes = fte3600_auth_probe_encode (probe);
  g_autoptr(GBytes) query_again = fte3600_auth_probe_encode (probe);
  g_assert_nonnull (query_bytes);
  g_assert_true (g_bytes_equal (query_bytes, query_again));
  g_assert_false (g_bytes_equal (query_bytes, wire));
  g_assert_null (fte3600_auth_template_decode (query_bytes, &error));
  g_assert_error (error, FTE3600_AUTH_ERROR, FTE3600_AUTH_INVALID_TEMPLATE);
  g_clear_error (&error);
  g_assert_true (fte3600_auth_match (probe, loaded, &result, &error));
  g_assert_no_error (error);
  g_assert_true (result.matched);
  g_assert_cmpuint (result.inliers, >=, 5);
  g_assert_cmpuint (result.mutual_matches, >=, result.inliers);
  g_assert_false (result.uninformative);
  g_test_message ("chip=%04x geometry=%ux%u bytes=%zu heldout_inliers=%u mutual=%u",
                  test->chip, test->width, test->height, g_bytes_get_size (wire), result.inliers,
                  result.mutual_matches);

  fte3600_auth_test_image (image, identity.width, identity.height, FTE3600_AUTH_TEST_OTHER_SEED, 0);
  g_autoptr(Fte3600AuthProbe) other = fte3600_auth_probe_new (&identity, image, size, identity.width, &error);
  g_assert_no_error (error);
  g_assert_true (fte3600_auth_match (other, loaded, &result, &error));
  g_assert_no_error (error);
  g_assert_false (result.matched);
  /* Only strip profiles may call a non-match uninformative, and only when the
   * touch overlapped no enrolled sample. */
  g_assert_cmpint (result.uninformative, ==,
                   test->width <= 40 && result.mutual_matches < FTE3600_AUTH_OVERLAP_MUTUAL);
  g_test_message ("chip=%04x other_finger mutual=%u uninformative=%d",
                  test->chip, result.mutual_matches, result.uninformative);
  memset (image, 128, size);
  g_assert_null (fte3600_auth_probe_new (&identity, image, size, identity.width, &error));
  g_assert_error (error, FTE3600_AUTH_ERROR, FTE3600_AUTH_LOW_QUALITY);
}

static void
test_rejects (void)
{
  Fte3600AuthIdentity identity = identity_for (&identities[2]);

  g_autoptr(GError) error = NULL;
  g_autoptr(Fte3600AuthEnrollment) enrollment = fte3600_auth_enrollment_new (&identity, NULL);
  g_assert_null (fte3600_auth_enrollment_finish (enrollment, &error));
  g_assert_error (error, FTE3600_AUTH_ERROR, FTE3600_AUTH_INCOMPLETE);
  g_clear_error (&error);
  const gsize size = identity.width * identity.height, stride = identity.width + 7;
  g_autofree guint8 *image = g_malloc (size);
  g_autofree guint8 *padded = g_malloc0 (stride * identity.height);
  fte3600_auth_test_image (image, identity.width, identity.height, FTE3600_AUTH_TEST_SEED, 0);
  guint count;
  g_assert_false (fte3600_auth_enrollment_add (enrollment, image, size - 1, identity.width, &count, &error));
  g_assert_error (error, FTE3600_AUTH_ERROR, FTE3600_AUTH_INVALID_ARGUMENT);
  g_clear_error (&error);
  g_assert_cmpuint (count, ==, 0);
  g_assert_false (fte3600_auth_enrollment_add (enrollment, image, size, G_MAXSIZE, NULL, &error));
  g_assert_error (error, FTE3600_AUTH_ERROR, FTE3600_AUTH_INVALID_ARGUMENT);
  g_clear_error (&error);
  g_assert_true (fte3600_auth_enrollment_add (enrollment, image, size, identity.width, &count, &error));
  g_assert_cmpuint (count, ==, 1);
  g_assert_false (fte3600_auth_enrollment_add (enrollment, image, size, identity.width, &count, &error));
  g_assert_error (error, FTE3600_AUTH_ERROR, FTE3600_AUTH_DUPLICATE);
  g_clear_error (&error);
  for (guint y = 0; y < identity.height; y++)
    memcpy (padded + y * stride, image + y * identity.width, identity.width);
  g_assert_false (fte3600_auth_enrollment_add (enrollment, padded, stride * (identity.height - 1) + identity.width,
                                               stride, &count, &error));
  g_assert_error (error, FTE3600_AUTH_ERROR, FTE3600_AUTH_DUPLICATE);
  g_clear_error (&error);
  fte3600_auth_test_image (image, identity.width, identity.height, FTE3600_AUTH_TEST_OTHER_SEED, 0);
  g_assert_false (fte3600_auth_enrollment_add (enrollment, image, size, identity.width, &count, &error));
  g_assert_error (error, FTE3600_AUTH_ERROR, FTE3600_AUTH_INCONSISTENT);
  g_clear_error (&error);
  memset (image, 128, size);
  g_assert_false (fte3600_auth_enrollment_add (enrollment, image, size, identity.width, &count, &error));
  g_assert_error (error, FTE3600_AUTH_ERROR, FTE3600_AUTH_LOW_QUALITY);
  g_clear_error (&error);
  g_assert_cmpuint (fte3600_auth_enrollment_count (enrollment), ==, 1);
  identity.processing_version = 1;
  g_assert_null (fte3600_auth_enrollment_new (&identity, &error));
  g_assert_error (error, FTE3600_AUTH_ERROR, FTE3600_AUTH_UNSUPPORTED);
  g_clear_error (&error);
  identity.processing_version = FTE3600_AUTH_PROCESSING_VERSION;
  identity.width = 40;
  identity.height = 196;
  g_assert_null (fte3600_auth_enrollment_new (&identity, &error));
  g_assert_error (error, FTE3600_AUTH_ERROR, FTE3600_AUTH_UNSUPPORTED);
}

static void
test_identity (void)
{
  Fte3600AuthIdentity identity;
  struct fte3600_device_status status = {.state = FTE3600_DEVICE_IDLE, .model = FTE3600_MODEL_FT9769,
                                         .chip_id = 0x9391, .width = 40, .height = 196, .startup_complete = 1, .calibration_complete = 1};

  g_assert_true (fte3600_auth_identity_from_status (&status, &identity, NULL));
  g_assert_cmpuint (identity.chip_id, ==, 0x9391);
  status.chip_id = 0x9769;
  g_assert_false (fte3600_auth_identity_from_status (&status, &identity, NULL));
  status.chip_id = 0x9395;
  g_assert_false (fte3600_auth_identity_from_status (&status, &identity, NULL));
  status.width = 36;
  status.height = 176;
  g_assert_true (fte3600_auth_identity_from_status (&status, &identity, NULL));
  status.calibration_complete = 0;
  g_assert_false (fte3600_auth_identity_from_status (&status, &identity, NULL));
  g_assert_cmpuint (identity.width, ==, 0);
  status.calibration_complete = 1;
  status.model = FTE3600_MODEL_UNKNOWN;
  g_assert_false (fte3600_auth_identity_from_status (&status, &identity, NULL));
  status.model = FTE3600_MODEL_FT9769;
  status.state = FTE3600_DEVICE_FAULT;
  g_assert_false (fte3600_auth_identity_from_status (&status, &identity, NULL));
}

static void
test_persist_identify (void)
{
  Fte3600AuthIdentity identity = identity_for (&identities[2]);
  Fte3600AuthIdentity alien_identity = identity_for (&identities[4]);

  g_autoptr(Fte3600AuthTemplate) a = enroll (&identity, FTE3600_AUTH_TEST_SEED);
  g_autoptr(Fte3600AuthTemplate) b = enroll (&identity, FTE3600_AUTH_TEST_OTHER_SEED);
  g_autoptr(Fte3600AuthTemplate) alien = enroll (&alien_identity, FTE3600_AUTH_TEST_SEED);
  g_autoptr(GBytes) wire = fte3600_auth_template_encode (a);
  g_autoptr(GError) error = NULL;
  g_autofree gchar *directory = g_dir_make_tmp ("fte3600-auth-XXXXXX", &error);
  g_assert_no_error (error);
  g_autofree gchar *path = g_build_filename (directory, "template.bin", NULL);
  gsize size;
  const gchar *data = g_bytes_get_data (wire, &size);
  g_assert_true (g_file_set_contents (path, data, size, &error));
  g_assert_no_error (error);
  g_autofree gchar *disk = NULL;
  g_assert_true (g_file_get_contents (path, &disk, &size, &error));
  g_assert_no_error (error);
  g_autoptr(GBytes) from_disk = g_bytes_new (disk, size);
  g_autoptr(Fte3600AuthTemplate) reloaded = fte3600_auth_template_decode (from_disk, &error);
  g_assert_no_error (error);
  g_assert_cmpint (g_remove (path), ==, 0);
  g_assert_cmpint (g_rmdir (directory), ==, 0);
  guint8 image[64 * 80];
  fte3600_auth_test_image (image, 64, 80, FTE3600_AUTH_TEST_SEED, 12);
  g_autoptr(Fte3600AuthProbe) probe = fte3600_auth_probe_new (&identity, image, sizeof image, 64, &error);
  g_assert_no_error (error);
  Fte3600AuthMatch match;
  g_assert_false (fte3600_auth_match (probe, alien, &match, &error));
  g_assert_false (match.matched);
  g_assert_error (error, FTE3600_AUTH_ERROR, FTE3600_AUTH_PROFILE_MISMATCH);
  g_clear_error (&error);
  g_autoptr(GPtrArray) gallery = g_ptr_array_new ();
  g_ptr_array_add (gallery, b);
  g_ptr_array_add (gallery, alien);
  g_ptr_array_add (gallery, reloaded);
  gint best;
  g_assert_true (fte3600_auth_identify (probe, gallery, &best, &match, &error));
  g_assert_no_error (error);
  g_assert_true (match.matched);
  g_assert_cmpint (best, ==, 2);
  for (guint i = 0; i < 4; i++)
    {
      g_assert_true (fte3600_auth_match (probe, reloaded, &match, &error));
      g_assert_no_error (error);
      g_assert_true (match.matched);
    }
  g_ptr_array_set_size (gallery, 2);
  g_assert_true (fte3600_auth_identify (probe, gallery, &best, &match, &error));
  g_assert_no_error (error);
  g_assert_false (match.matched);
  g_assert_cmpint (best, ==, -1);
  g_ptr_array_add (gallery, reloaded);
  g_ptr_array_add (gallery, NULL);
  g_assert_false (fte3600_auth_identify (probe, gallery, &best, &match, &error));
  g_assert_error (error, FTE3600_AUTH_ERROR, FTE3600_AUTH_INVALID_ARGUMENT);
  g_assert_false (match.matched);
  g_assert_cmpint (best, ==, -1);
}

static void
test_strip_coverage (void)
{
  Fte3600AuthIdentity identity = identity_for (&identities[10]);
  g_autoptr(Fte3600AuthEnrollment) enrollment = fte3600_auth_enrollment_new (&identity, NULL);
  guint8 image[40 * 196];
  /* Independent fields stand for non-overlapping skin. This deliberately
   * demonstrates that coverage enrollment cannot enforce one finger. */
  for (guint i = 0; i < FTE3600_AUTH_ENROLL_SAMPLES; i++)
    {
      fte3600_auth_test_image (image, 40, 196,
                               i < 6 ? FTE3600_AUTH_TEST_SEED : FTE3600_AUTH_TEST_OTHER_SEED, i);
      g_assert_true (fte3600_auth_enrollment_add (enrollment, image, sizeof image, 40, NULL, NULL));
    }
  g_autoptr(Fte3600AuthTemplate) mixed = fte3600_auth_enrollment_finish (enrollment, NULL);
  g_autoptr(GBytes) bytes = fte3600_auth_template_encode (mixed);
  g_autoptr(Fte3600AuthTemplate) loaded = fte3600_auth_template_decode (bytes, NULL);
  g_assert_nonnull (loaded);
  for (guint i = 0; i < 2; i++)
    {
      fte3600_auth_test_image (image, 40, 196,
                               i ? FTE3600_AUTH_TEST_OTHER_SEED : FTE3600_AUTH_TEST_SEED, 12);
      g_autoptr(Fte3600AuthProbe) probe = fte3600_auth_probe_new (&identity, image, sizeof image, 40, NULL);
      Fte3600AuthMatch match;
      g_assert_true (fte3600_auth_match (probe, loaded, &match, NULL));
      g_assert_true (match.matched);
    }
  fte3600_auth_test_image (image, 40, 196, FTE3600_AUTH_TEST_OTHER_SEED, 12);
  g_autoptr(Fte3600AuthProbe) probe = fte3600_auth_probe_new (&identity, image, sizeof image, 40, NULL);
  g_autoptr(Fte3600AuthTemplate) original = enroll (&identity, FTE3600_AUTH_TEST_SEED);
  g_autoptr(GPtrArray) gallery = g_ptr_array_new ();
  Fte3600AuthMatch match;
  gint best;
  g_assert_true (fte3600_auth_identify (probe, gallery, &best, &match, NULL));
  g_assert_false (match.uninformative);
  g_assert_cmpint (best, ==, -1);
  g_ptr_array_add (gallery, original);
  g_assert_true (fte3600_auth_identify (probe, gallery, &best, &match, NULL));
  g_assert_false (match.matched);
  g_assert_true (match.uninformative);
  g_ptr_array_add (gallery, loaded);
  g_assert_true (fte3600_auth_identify (probe, gallery, &best, &match, NULL));
  g_assert_true (match.matched);
  g_assert_false (match.uninformative);
  g_assert_cmpint (best, ==, 1);
}

static void
test_best_match (void)
{
  Fte3600AuthIdentity identity = identity_for (&identities[2]);

  g_autoptr(Fte3600AuthTemplate) first = enroll (&identity, FTE3600_AUTH_TEST_SEED);
  g_autoptr(Fte3600AuthEnrollment) enrollment = fte3600_auth_enrollment_new (&identity, NULL);
  guint8 image[64 * 80];
  static const gint shifts[12][2] = {
    {2, 1}, {1, 1}, {2, 0}, {3, 1}, {2, 2}, {1, 0},
    {3, 0}, {1, 2}, {3, 2}, {2, -1}, {0, 1}, {4, 1}
  };
  for (guint i = 0; i < FTE3600_AUTH_ENROLL_SAMPLES; i++)
    {
      fte3600_auth_test_image_shift (image, 64, 80, FTE3600_AUTH_TEST_SEED, shifts[i][0], shifts[i][1]);
      g_assert_true (fte3600_auth_enrollment_add (enrollment, image, sizeof image, 64, NULL, NULL));
    }
  g_autoptr(Fte3600AuthTemplate) second = fte3600_auth_enrollment_finish (enrollment, NULL);
  fte3600_auth_test_image (image, 64, 80, FTE3600_AUTH_TEST_SEED, 12);
  g_autoptr(Fte3600AuthProbe) probe = fte3600_auth_probe_new (&identity, image, sizeof image, 64, NULL);
  Fte3600AuthMatch weaker, stronger, selected;
  g_assert_true (fte3600_auth_match (probe, first, &weaker, NULL));
  g_assert_true (fte3600_auth_match (probe, second, &stronger, NULL));
  g_assert_true (weaker.matched);
  g_assert_true (stronger.matched);
  g_assert_true (stronger.inliers > weaker.inliers ||
                 (stronger.inliers == weaker.inliers && stronger.mean_hamming < weaker.mean_hamming) ||
                 (stronger.inliers == weaker.inliers && stronger.mean_hamming == weaker.mean_hamming &&
                  stronger.rms_error < weaker.rms_error));
  g_autoptr(GPtrArray) gallery = g_ptr_array_new ();
  g_ptr_array_add (gallery, first);
  g_ptr_array_add (gallery, second);
  g_ptr_array_add (gallery, second);
  gint best;
  g_assert_true (fte3600_auth_identify (probe, gallery, &best, &selected, NULL));
  g_assert_cmpint (best, ==, 1);
  g_assert_cmpuint (selected.inliers, ==, stronger.inliers);
  g_assert_cmpfloat (selected.mean_hamming, ==, stronger.mean_hamming);
}

static void
expect_invalid (const guint8 *data, gsize size)
{
  g_autoptr(GBytes) bytes = g_bytes_new_static (data, size);
  g_autoptr(GError) error = NULL;
  g_assert_null (fte3600_auth_template_decode (bytes, &error));
  g_assert_error (error, FTE3600_AUTH_ERROR, FTE3600_AUTH_INVALID_TEMPLATE);
}

static void
rehash (guint8 *data, gsize size)
{
  g_autoptr(GChecksum) checksum = g_checksum_new (G_CHECKSUM_SHA256);
  gsize n = 32;
  g_checksum_update (checksum, data, size - 32);
  g_checksum_get_digest (checksum, data + size - 32, &n);
}

static void
put32 (guint8 *p, guint32 value)
{
  p[0] = value >> 24;
  p[1] = value >> 16;
  p[2] = value >> 8;
  p[3] = value;
}

static void
putfloat (guint8 *p, gfloat value)
{
  guint32 bits;

  memcpy (&bits, &value, sizeof bits);
  put32 (p, bits);
}

/* Enlarge a real mathematical-field sample to the maximum wire record with
 * deterministic synthetic descriptors/locations; translate eight records by
 * distinct subpixels. This is parser/matcher load, never biometric evidence. */
static GBytes *
maximum_template_bytes (void)
{
  Fte3600AuthIdentity identity = identity_for (&identities[2]);

  g_autoptr(Fte3600AuthTemplate) print = enroll (&identity, FTE3600_AUTH_TEST_SEED);
  g_autoptr(GBytes) wire = fte3600_auth_template_encode (print);
  const guint8 *source = g_bytes_get_data (wire, NULL);
  const guint count = ((guint) source[40] << 8) | source[41];
  const guint record_size = 4 + 44 * FPI_BRISK_MAX_FEATURES;
  const guint size = 40 + FTE3600_AUTH_ENROLL_SAMPLES * record_size + 32;
  guint8 *data = g_malloc0 (size);
  memcpy (data, source, 40);
  put32 (data + 12, size);
  guint8 first[44 * FPI_BRISK_MAX_FEATURES];
  memcpy (first, source + 44, count * 44);
  guint32 random = 0x763914abu;
  for (guint i = count; i < FPI_BRISK_MAX_FEATURES; i++)
    {
      guint8 *feature = first + 44 * i;
      putfloat (feature, 2 + 3 * (i % 16));
      putfloat (feature + 4, 4 + 7 * (i / 16));
      putfloat (feature + 8, 0);
      for (guint j = 0; j < 32; j++)
        feature[12 + j] = fte3600_auth_test_random (&random);
    }
  for (guint i = 0; i < FTE3600_AUTH_ENROLL_SAMPLES; i++)
    {
      guint8 *record = data + 40 + record_size * i;
      record[0] = FPI_BRISK_MAX_FEATURES >> 8;
      record[1] = FPI_BRISK_MAX_FEATURES;
      memcpy (record + 4, first, sizeof first);
      for (guint j = 0; j < FPI_BRISK_MAX_FEATURES; j++)
        {
          guint8 *p = record + 4 + 44 * j;
          guint32 bits = ((guint32) p[0] << 24) | ((guint32) p[1] << 16) | ((guint32) p[2] << 8) | p[3];
          gfloat x;
          memcpy (&x, &bits, sizeof x);
          putfloat (p, x + i * 0.125f);
        }
    }
  rehash (data, size);
  return g_bytes_new_take (data, size);
}

static gboolean
cancel_at_deadline (gpointer data)
{
  return g_get_monotonic_time () >= *(const gint64 *) data;
}

static void
test_performance (void)
{
  g_autoptr(GBytes) bytes = maximum_template_bytes ();
  g_autoptr(GError) error = NULL;
  const gint64 decode_start = g_get_monotonic_time ();
  g_autoptr(Fte3600AuthTemplate) print = fte3600_auth_template_decode (bytes, &error);
  g_assert_no_error (error);
  g_assert_nonnull (print);
  g_test_message ("maximum template: features=160 samples=%u bytes=%zu decode_ms=%.3f",
                  FTE3600_AUTH_ENROLL_SAMPLES,
                  g_bytes_get_size (bytes), (g_get_monotonic_time () - decode_start) / 1000.0);
  Fte3600AuthIdentity identity = identity_for (&identities[2]);
  guint8 image[64 * 80];
  fte3600_auth_test_image (image, 64, 80, FTE3600_AUTH_TEST_SEED, 12);
  g_autoptr(Fte3600AuthProbe) probe = fte3600_auth_probe_new (&identity, image, sizeof image, 64, &error);
  g_assert_no_error (error);
  g_autoptr(GPtrArray) gallery = g_ptr_array_new ();
  const guint sizes[] = {1, 10, 100};
  for (guint i = 0; i < G_N_ELEMENTS (sizes); i++)
    {
      while (gallery->len < sizes[i])
        g_ptr_array_add (gallery, print);
      gint best;
      Fte3600AuthMatch match;
      const gint64 start = g_get_monotonic_time ();
      g_assert_true (fte3600_auth_identify (probe, gallery, &best, &match, &error));
      g_assert_no_error (error);
      g_assert_true (match.matched);
      g_assert_cmpint (best, ==, 0);
      g_test_message ("validated immutable gallery=%u identify_ms=%.3f inliers=%u",
                      sizes[i], (g_get_monotonic_time () - start) / 1000.0, match.inliers);
    }
  gsize size;
  const guint8 *source = g_bytes_get_data (bytes, &size);
  g_autofree guint8 *permuted = g_memdup2 (source, size);
  const guint record_size = 4 + 44 * FPI_BRISK_MAX_FEATURES;
  for (guint sample = 0; sample < FTE3600_AUTH_ENROLL_SAMPLES; sample++)
    for (guint i = 0; i < FPI_BRISK_MAX_FEATURES; i++)
      memcpy (permuted + 44 + sample * record_size + i * 44,
              source + 44 + ((i + sample) % FPI_BRISK_MAX_FEATURES) * 44, 44);
  rehash (permuted, size);
  g_autoptr(GBytes) duplicate = g_bytes_new_static (permuted, size);
  const gint64 start = g_get_monotonic_time ();
  g_autoptr(Fte3600AuthTemplate) rejected = fte3600_auth_template_decode (duplicate, &error);
  g_test_message ("maximum permuted duplicate: accepted=%d decode_ms=%.3f",
                  rejected != NULL, (g_get_monotonic_time () - start) / 1000.0);
  g_assert_null (rejected);
  g_assert_error (error, FTE3600_AUTH_ERROR, FTE3600_AUTH_INVALID_TEMPLATE);
  g_clear_error (&error);
  gint64 started = g_get_monotonic_time ();
  gint64 deadline = started + 5000;
  g_assert_null (fte3600_auth_template_decode_full (bytes, cancel_at_deadline, &deadline, &error));
  g_assert_error (error, FTE3600_AUTH_ERROR, FTE3600_AUTH_CANCELLED);
  g_test_message ("max decode cancel after5ms: return_ms=%.3f",
                  (g_get_monotonic_time () - started) / 1000.0);
  g_clear_error (&error);
  gint best;
  Fte3600AuthMatch match;
  started = g_get_monotonic_time ();
  deadline = started + 5000;
  g_assert_false (fte3600_auth_identify_full (probe, gallery, &best, &match,
                                              cancel_at_deadline, &deadline, &error));
  g_assert_error (error, FTE3600_AUTH_ERROR, FTE3600_AUTH_CANCELLED);
  g_assert_cmpint (best, ==, -1);
  g_assert_false (match.matched);
  g_test_message ("gallery100 cancel after5ms: return_ms=%.3f",
                  (g_get_monotonic_time () - started) / 1000.0);
}

static void
test_feature_multisets (void)
{
  Fte3600AuthIdentity identity = identity_for (&identities[2]);

  g_autoptr(Fte3600AuthTemplate) print = enroll (&identity, FTE3600_AUTH_TEST_SEED);
  g_autoptr(GBytes) wire = fte3600_auth_template_encode (print);
  gsize size;
  const guint8 *source = g_bytes_get_data (wire, &size);
  const guint count = ((guint) source[40] << 8) | source[41];
  const guint repeated_record = 4 + 44 * count;
  const guint repeated_size = 40 + FTE3600_AUTH_ENROLL_SAMPLES * repeated_record + 32;
  g_autofree guint8 *data = g_malloc0 (FTE3600_AUTH_MAX_TEMPLATE_BYTES);
  for (guint mode = 0; mode < 3; mode++)
    {
      memcpy (data, source, 40);
      put32 (data + 12, repeated_size);
      for (guint sample = 0; sample < FTE3600_AUTH_ENROLL_SAMPLES; sample++)
        {
          guint8 *record = data + 40 + sample * repeated_record;
          memcpy (record, source + 40, 4);
          for (guint i = 0; i < count; i++)
            {
              guint8 *feature = record + 4 + i * 44;
              const guint j = mode ? (i + sample) % count : i;
              memcpy (feature, source + 44 + j * 44, 44);
              if (mode == 2)
                /* +0 and -0 are numerically identical, including after a shuffle. */
                put32 (feature + 8, j < 3 && (sample & (1u << j)) ? 0x80000000u : 0);
            }
        }
      rehash (data, repeated_size);
      expect_invalid (data, repeated_size);
    }
  /* Feature ordering remains unrestricted in a valid template;
   * decode/re-encode must preserve exactly the order presented to BRISK. */
  memcpy (data, source, size);
  gsize at = 40;
  for (guint sample = 0; sample < FTE3600_AUTH_ENROLL_SAMPLES; sample++)
    {
      const guint n = ((guint) source[at] << 8) | source[at + 1];
      for (guint i = 0; i < n; i++)
        memcpy (data + at + 4 + i * 44, source + at + 4 + ((i + sample + 1) % n) * 44, 44);
      at += 4 + n * 44;
    }
  rehash (data, size);
  g_autoptr(GBytes) reordered = g_bytes_new (data, size);
  g_autoptr(GError) error = NULL;
  g_autoptr(Fte3600AuthTemplate) valid = fte3600_auth_template_decode (reordered, &error);
  g_assert_no_error (error);
  g_assert_nonnull (valid);
  g_autoptr(GBytes) reencoded = fte3600_auth_template_encode (valid);
  g_assert_true (g_bytes_equal (reencoded, reordered));
  /* All twelve records have the same unique members and count, but different
   * multiplicities of the first two members; these are distinct multisets. */
  const guint multiplicity_count = count + FTE3600_AUTH_ENROLL_SAMPLES;
  const guint multiplicity_record = 4 + multiplicity_count * 44;
  const guint multiplicity_size = 40 + FTE3600_AUTH_ENROLL_SAMPLES * multiplicity_record + 32;
  g_assert_cmpuint (multiplicity_count, <=, FPI_BRISK_MAX_FEATURES);
  memcpy (data, source, 40);
  put32 (data + 12, multiplicity_size);
  for (guint sample = 0; sample < FTE3600_AUTH_ENROLL_SAMPLES; sample++)
    {
      guint8 *record = data + 40 + sample * multiplicity_record;
      record[0] = multiplicity_count >> 8;
      record[1] = multiplicity_count;
      record[2] = record[3] = 0;
      memcpy (record + 4, source + 44, count * 44);
      for (guint i = 0; i < FTE3600_AUTH_ENROLL_SAMPLES; i++)
        memcpy (record + 4 + (count + i) * 44, source + 44 + (i < sample ? 0 : 44), 44);
    }
  rehash (data, multiplicity_size);
  g_autoptr(GBytes) multiplicities = g_bytes_new (data, multiplicity_size);
  g_autoptr(Fte3600AuthTemplate) distinct = fte3600_auth_template_decode (multiplicities, &error);
  g_assert_no_error (error);
  g_assert_nonnull (distinct);
  /* Reordered duplicate with repeated members must still be rejected. */
  guint8 *last = data + 40 + (FTE3600_AUTH_ENROLL_SAMPLES - 1u) * multiplicity_record;
  for (guint i = 0; i < multiplicity_count; i++)
    memcpy (last + 4 + i * 44, data + 44 + ((i + 13) % multiplicity_count) * 44, 44);
  rehash (data, multiplicity_size);
  expect_invalid (data, multiplicity_size);
}

typedef struct { guint checks, cancel_at;
} Cancellation;

static gboolean
cancel_after (gpointer data)
{
  Cancellation *cancel = data;

  return ++cancel->checks >= cancel->cancel_at;
}

static void
assert_cancelled (GError **error)
{
  g_assert_error (*error, FTE3600_AUTH_ERROR, FTE3600_AUTH_CANCELLED);
  g_clear_error (error);
}

static void
test_cancellation (void)
{
  Fte3600AuthIdentity identity = identity_for (&identities[2]);

  g_autoptr(GBytes) bytes = maximum_template_bytes ();
  g_autoptr(GError) error = NULL;
  Cancellation cancel = {0, G_MAXUINT};
  g_autoptr(Fte3600AuthTemplate) print =
    fte3600_auth_template_decode_full (bytes, cancel_after, &cancel, &error);
  g_assert_no_error (error);
  g_assert_nonnull (print);
  /* Every decode boundary, including after the final strict match, must
   * release all transient state and return no usable partially checked print. */
  const guint decode_checks = cancel.checks;
  for (guint boundary = 1; boundary <= decode_checks; boundary++)
    {
      cancel = (Cancellation){0, boundary};
      g_assert_null (fte3600_auth_template_decode_full (bytes, cancel_after, &cancel, &error));
      assert_cancelled (&error);
    }
  guint8 image[64 * 80];
  fte3600_auth_test_image (image, 64, 80, FTE3600_AUTH_TEST_SEED, 12);
  cancel = (Cancellation){0, G_MAXUINT};
  g_autoptr(Fte3600AuthProbe) probe =
    fte3600_auth_probe_new_full (&identity, image, sizeof image, 64, cancel_after, &cancel, &error);
  g_assert_no_error (error);
  g_assert_nonnull (probe);
  const guint probe_checks = cancel.checks;
  for (guint boundary = 1; boundary <= probe_checks; boundary++)
    {
      cancel = (Cancellation){0, boundary};
      g_assert_null (fte3600_auth_probe_new_full (&identity, image, sizeof image, 64,
                                                  cancel_after, &cancel, &error));
      assert_cancelled (&error);
    }
  cancel = (Cancellation){0, G_MAXUINT};
  Fte3600AuthMatch match;
  g_assert_true (fte3600_auth_match_full (probe, print, &match, cancel_after, &cancel, &error));
  g_assert_no_error (error);
  g_assert_true (match.matched);
  const guint match_checks = cancel.checks;
  for (guint boundary = 1; boundary <= match_checks; boundary++)
    {
      cancel = (Cancellation){0, boundary};
      match = (Fte3600AuthMatch){TRUE, 99, 1, 1};
      g_assert_false (fte3600_auth_match_full (probe, print, &match, cancel_after, &cancel, &error));
      assert_cancelled (&error);
      g_assert_false (match.matched);
      g_assert_cmpuint (match.inliers, ==, 0);
      g_assert_cmpfloat (match.mean_hamming, ==, 0);
      g_assert_cmpfloat (match.rms_error, ==, 0);
    }
  g_autoptr(GPtrArray) gallery = g_ptr_array_new ();
  g_ptr_array_add (gallery, print);
  g_ptr_array_add (gallery, print);
  gint best;
  cancel = (Cancellation){0, G_MAXUINT};
  g_assert_true (fte3600_auth_identify_full (probe, gallery, &best, &match, cancel_after, &cancel, &error));
  g_assert_no_error (error);
  g_assert_cmpint (best, ==, 0);
  const guint identify_checks = cancel.checks;
  for (guint boundary = 1; boundary <= identify_checks; boundary++)
    {
      cancel = (Cancellation){0, boundary};
      best = 99;
      match.matched = TRUE;
      g_assert_false (fte3600_auth_identify_full (probe, gallery, &best, &match,
                                                  cancel_after, &cancel, &error));
      assert_cancelled (&error);
      g_assert_cmpint (best, ==, -1);
      g_assert_false (match.matched);
    }
  /* Cancelled second-frame work must never advance the accepted count. */
  g_autoptr(Fte3600AuthEnrollment) enrollment = fte3600_auth_enrollment_new (&identity, NULL);
  fte3600_auth_test_image (image, 64, 80, FTE3600_AUTH_TEST_SEED, 0);
  g_assert_true (fte3600_auth_enrollment_add (enrollment, image, sizeof image, 64, NULL, NULL));
  fte3600_auth_test_image (image, 64, 80, FTE3600_AUTH_TEST_SEED, 1);
  for (guint boundary = 1; ; boundary++)
    {
      guint completed = 99;
      cancel = (Cancellation){0, boundary};
      if (fte3600_auth_enrollment_add_full (enrollment, image, sizeof image, 64, &completed,
                                            cancel_after, &cancel, &error))
        {
          g_assert_no_error (error);
          g_assert_cmpuint (completed, ==, 2);
          break;
        }
      assert_cancelled (&error);
      g_assert_cmpuint (completed, ==, 1);
      g_assert_cmpuint (fte3600_auth_enrollment_count (enrollment), ==, 1);
    }
  /* Independent owners can release their references without invalidating
   * an active immutable gallery; its exact bytes survive all operations. */
  g_autoptr(Fte3600AuthTemplate) retained = fte3600_auth_template_ref (print);
  g_assert_true (retained == print);
  g_clear_pointer (&print, fte3600_auth_template_free);
  g_autoptr(GBytes) after = fte3600_auth_template_encode (retained);
  g_assert_true (g_bytes_equal (bytes, after));
  g_assert_true (fte3600_auth_match (probe, retained, &match, &error));
  g_assert_no_error (error);
  g_assert_true (match.matched);
}

static void
test_codec (void)
{
  Fte3600AuthIdentity identity = identity_for (&identities[2]);

  g_autoptr(Fte3600AuthTemplate) print = enroll (&identity, FTE3600_AUTH_TEST_SEED);
  g_autoptr(GBytes) wire = fte3600_auth_template_encode (print);
  gsize size;
  const guint8 *data = g_bytes_get_data (wire, &size);
  g_autofree guint8 *mutant = g_malloc (FTE3600_AUTH_MAX_TEMPLATE_BYTES + 1);
  for (gsize n = 0; n < size; n++)
    expect_invalid (data, n);
  for (guint offset = 0; offset < 40; offset++)
    {
      memcpy (mutant, data, size);
      mutant[offset] ^= 0xff;
      rehash (mutant, size);
      expect_invalid (mutant, size);
    }
  memset (mutant, 0, FTE3600_AUTH_MAX_TEMPLATE_BYTES + 1);
  expect_invalid (mutant, FTE3600_AUTH_MAX_TEMPLATE_BYTES + 1);
  /* Counts/reserved, quiet NaN, +infinity, negative/out-of-bounds x and
   * orientation. Recompute the checksum to actually exercise the parser. */
  static const struct {guint  offset;
                       guint8 bytes[4];
  } mutations[] = {
    {40, {0xff, 0xff, 0, 0}}, {40, {0, 0, 0, 0}}, {40, {0, 5, 0, 1}},
    {44, {0x7f, 0xc0, 0, 0}}, {44, {0x7f, 0x80, 0, 0}}, {44, {0xbf, 0x80, 0, 0}},
    {44, {0x43, 0x80, 0, 0}}, {52, {0x40, 0x80, 0, 0}}
  };
  for (guint i = 0; i < G_N_ELEMENTS (mutations); i++)
    {
      memcpy (mutant, data, size);
      memcpy (mutant + mutations[i].offset, mutations[i].bytes, 4);
      rehash (mutant, size);
      expect_invalid (mutant, size);
    }
  /* Broken descriptor evidence passes structural parsing but cannot become
   * an enrollment anchor or accepted template. */
  memcpy (mutant, data, size);
  const guint count = ((guint) data[40] << 8) | data[41];
  for (guint i = 0; i < count; i++)
    memset (mutant + 44 + 44 * i + 12, 0, 32);
  rehash (mutant, size);
  expect_invalid (mutant, size);
  /* Actual duplicate record, with corrected byte length and digest. */
  const gsize record_size = 4 + count * 44;
  const gsize duplicate_size = 40 + record_size * FTE3600_AUTH_ENROLL_SAMPLES + 32;
  memcpy (mutant, data, 40);
  mutant[12] = duplicate_size >> 24;
  mutant[13] = duplicate_size >> 16;
  mutant[14] = duplicate_size >> 8;
  mutant[15] = duplicate_size;
  for (guint i = 0; i < FTE3600_AUTH_ENROLL_SAMPLES; i++)
    memcpy (mutant + 40 + i * record_size, data + 40, record_size);
  rehash (mutant, duplicate_size);
  expect_invalid (mutant, duplicate_size);
  for (guint i = 0; i < 64; i++)
    {
      memcpy (mutant, data, size);
      mutant[40 + (i * 331u % (size - 40))] ^= 0x80;
      expect_invalid (mutant, size);
    }
}

static void
test_clahe_determinism_and_blank_guard (void)
{
  const guint width = 64, height = 80;
  const gsize size = width * height, stride = width + 5;
  g_autofree guint8 *src = g_malloc (size);
  g_autofree guint8 *src_copy = g_malloc (size);
  g_autofree guint8 *padded = g_malloc0 (stride * height);
  g_autofree guint8 *out_a = g_malloc (size);
  g_autofree guint8 *out_b = g_malloc (size);
  g_autofree guint8 *out_padded = g_malloc (size);

  fte3600_auth_test_image (src, width, height, FTE3600_AUTH_TEST_SEED, 0);
  memcpy (src_copy, src, size);
  for (guint y = 0; y < height; y++)
    memcpy (padded + y * stride, src + y * width, width);

  g_assert_true (fte3600_auth_preprocess_clahe (src, size, width, width, height, out_a));
  g_assert_true (fte3600_auth_preprocess_clahe (src, size, width, width, height, out_b));
  g_assert_true (fte3600_auth_preprocess_clahe (padded, stride * height, stride, width, height, out_padded));
  /* Input buffer must never be mutated, and packed vs strided outputs must match bit-for-bit. */
  g_assert_cmpmem (src, size, src_copy, size);
  g_assert_cmpmem (out_a, size, out_b, size);
  g_assert_cmpmem (out_a, size, out_padded, size);

  /* Near-uniform / blank frames (max - min < 12) must pass through unchanged
   * so blank frames are never amplified into synthetic features. */
  guint8 flat[64 * 80];
  for (gsize i = 0; i < sizeof flat; i++)
    flat[i] = 120u + (guint8) (i % 10u);
  g_assert_true (fte3600_auth_preprocess_clahe (flat, sizeof flat, 64, 64, 80, out_a));
  g_assert_cmpmem (out_a, sizeof flat, flat, sizeof flat);

  Fte3600AuthIdentity identity = identity_for (&identities[2]);
  g_autoptr(GError) error = NULL;
  memset (flat, 128, sizeof flat);
  g_assert_null (fte3600_auth_probe_new (&identity, flat, sizeof flat, 64, &error));
  g_assert_error (error, FTE3600_AUTH_ERROR, FTE3600_AUTH_LOW_QUALITY);
  g_clear_error (&error);

  /* Invalid bounds/stride must be rejected cleanly. */
  g_assert_false (fte3600_auth_preprocess_clahe (NULL, size, width, width, height, out_a));
  g_assert_false (fte3600_auth_preprocess_clahe (src, size - 1, width, width, height, out_a));
  g_assert_false (fte3600_auth_preprocess_clahe (src, size, width - 1, width, height, out_a));
}

static void
test_clahe_low_contrast_extraction (void)
{
  const guint width = 64, height = 80;
  const gsize size = width * height;
  guint8 raw[64 * 80];
  guint8 clahe[64 * 80];

  /* Build a synthetic 64x80 ridge/spot field with severe uneven lighting and
   * compressed local amplitude so raw extraction misses features. */
  fte3600_auth_test_image (raw, width, height, FTE3600_AUTH_TEST_SEED, 0);
  for (guint y = 0; y < height; y++)
    for (guint x = 0; x < width; x++)
      {
        const gdouble centered = ((gdouble) raw[y * width + x] - 128.0) * 0.14;
        const gdouble bias = 50.0 + 130.0 * ((gdouble) x / (width - 1)) * 0.65 +
                             45.0 * ((gdouble) y / (height - 1));
        raw[y * width + x] = (guint8) CLAMP (floor (bias + centered + 0.5), 1, 254);
      }

  FpiBriskImage raw_img = { raw, size, width, height, width };
  FpiBriskFeatureSet raw_features = { 0 };
  FpiBriskStatus raw_status = fpi_brisk_extract (&raw_img, &raw_features);

  g_assert_true (fte3600_auth_preprocess_clahe (raw, size, width, width, height, clahe));
  FpiBriskImage clahe_img = { clahe, size, width, height, width };
  FpiBriskFeatureSet clahe_features = { 0 };
  FpiBriskStatus clahe_status = fpi_brisk_extract (&clahe_img, &clahe_features);

  g_test_message ("low-contrast 64x80: raw_status=%d raw_n=%u clahe_status=%d clahe_n=%u",
                  (int) raw_status, raw_features.n_features,
                  (int) clahe_status, clahe_features.n_features);
  g_assert_cmpint (clahe_status, ==, FPI_BRISK_OK);
  g_assert_true (raw_status != FPI_BRISK_OK || clahe_features.n_features > raw_features.n_features);
}

static void
test_processing_version_rejected (void)
{
  Fte3600AuthIdentity identity = identity_for (&identities[2]);
  g_autoptr(Fte3600AuthTemplate) print = enroll (&identity, FTE3600_AUTH_TEST_SEED);
  g_autoptr(GBytes) wire = fte3600_auth_template_encode (print);
  gsize size = 0;
  const guint8 *data = g_bytes_get_data (wire, &size);
  g_autofree guint8 *legacy = g_memdup2 (data, size);

  /* Mutate processing_version (bytes 18..19) back to 0x1001u and rehash. */
  legacy[18] = 0x10;
  legacy[19] = 0x01;
  rehash (legacy, size);
  expect_invalid (legacy, size);
}

static void
test_two_tier_enrollment_gate (void)
{
  Fte3600AuthIdentity identity = identity_for (&identities[2]);
  g_autoptr(GError) error = NULL;
  g_autoptr(Fte3600AuthEnrollment) enrollment = fte3600_auth_enrollment_new (&identity, &error);
  g_assert_no_error (error);

  guint8 anchor[64 * 80];
  fte3600_auth_test_image (anchor, 64, 80, FTE3600_AUTH_TEST_SEED, 0);
  guint count = 0;
  g_assert_true (fte3600_auth_enrollment_add (enrollment, anchor, sizeof anchor, 64, &count, &error));
  g_assert_no_error (error);
  g_assert_cmpuint (count, ==, 1);

  /* Create a shifted/partially degraded same-finger 64x80 capture by taking a
   * larger translation and masking one strip to 128 so strict verification
   * (inlier_ratio / spatial spread / error) fails while the relaxed two-tier
   * enrollment consistency chain passes. */
  guint8 anchor_clahe[64 * 80];
  g_assert_true (fte3600_auth_preprocess_clahe (anchor, sizeof anchor, 64, 64, 80, anchor_clahe));
  FpiBriskImage anchor_img = { anchor_clahe, sizeof anchor_clahe, 64, 80, 64 };
  FpiBriskFeatureSet anchor_features = { 0 };
  g_assert_cmpint (fpi_brisk_extract (&anchor_img, &anchor_features), ==, FPI_BRISK_OK);

  guint8 degraded[64 * 80];
  gboolean found_tier2 = FALSE;
  for (guint step = 0; step <= 20 && !found_tier2; step++)
    {
      const gdouble scale = 1.100 + step * 0.005;
      for (gint dx = -2; dx <= 2 && !found_tier2; dx++)
        {
          for (guint y = 0; y < 80; y++)
            for (guint x = 0; x < 64; x++)
              {
                const gint sx = CLAMP ((gint) floor (32.0 + (x - 32.0 - dx) / scale + 0.5), 0, 63);
                const gint sy = CLAMP ((gint) floor (40.0 + (y - 40.0) / scale + 0.5), 0, 79);
                degraded[y * 64 + x] = anchor[sy * 64 + sx];
              }

          guint8 deg_clahe[64 * 80];
          g_assert_true (fte3600_auth_preprocess_clahe (degraded, sizeof degraded, 64, 64, 80, deg_clahe));
          FpiBriskImage deg_img = { deg_clahe, sizeof deg_clahe, 64, 80, 64 };
          FpiBriskFeatureSet deg_features = { 0 };
          if (fpi_brisk_extract (&deg_img, &deg_features) != FPI_BRISK_OK)
            continue;
          FpiBriskMatchEvidence self_ev = { 0 };
          if (fpi_brisk_match (&deg_features, 64, 80, &deg_features, 64, 80, &self_ev) != FPI_BRISK_OK ||
              self_ev.inliers < 5u || self_ev.query_min_variance < 3.0)
            continue;
          FpiBriskMatchEvidence ev = { 0 };
          FpiBriskStatus st = fpi_brisk_match (&deg_features, 64, 80, &anchor_features, 64, 80, &ev);
          if (st != FPI_BRISK_OK)
            continue;
          gboolean strict_ok = ev.inliers >= 5u && ev.inlier_ratio >= 0.20 &&
                               ev.median_error < 1.25 && ev.rms_error < 1.40 &&
                               ev.mean_hamming <= 60.0 && ev.query_min_variance >= 3.0 &&
                               ev.reference_min_variance >= 3.0 &&
                               ev.inliers - ev.competing_inliers >= 1;
          gboolean tier2_ok = ev.mutual_matches >= 6u && ev.inliers >= 4u &&
                              ev.competing_inliers < ev.inliers &&
                              ev.inlier_ratio >= 0.14 && ev.rms_error < 1.85 &&
                              ev.mean_hamming <= 68.0;
          if (!strict_ok && tier2_ok)
            {
              found_tier2 = TRUE;
              g_test_message ("tier2 candidate scale=%.3f dx=%d: mutual=%u inliers=%u comp=%u ratio=%.2f rms=%.2f med=%.2f ham=%.1f",
                              scale, dx, ev.mutual_matches, ev.inliers, ev.competing_inliers,
                              ev.inlier_ratio, ev.rms_error, ev.median_error, ev.mean_hamming);
            }
        }
    }
  g_assert_true (found_tier2);

  /* Enrollment must accept this degraded same-finger 64x80 capture via tier-2. */
  g_assert_true (fte3600_auth_enrollment_add (enrollment, degraded, sizeof degraded, 64, &count, &error));
  g_assert_no_error (error);
  g_assert_cmpuint (count, ==, 2);

  /* Impostor finger must STILL be rejected by consistent() during enrollment. */
  guint8 impostor[64 * 80];
  fte3600_auth_test_image (impostor, 64, 80, FTE3600_AUTH_TEST_OTHER_SEED, 0);
  g_assert_false (fte3600_auth_enrollment_add (enrollment, impostor, sizeof impostor, 64, &count, &error));
  g_assert_error (error, FTE3600_AUTH_ERROR, FTE3600_AUTH_INCONSISTENT);
  g_clear_error (&error);
  g_assert_cmpuint (count, ==, 2);

  /* Verification must STILL require strict authentication_accepted: enroll a full
   * 12-sample template and verify both that an impostor is rejected and that a
   * degraded same-finger probe failing authentication_accepted is rejected. */
  for (guint i = 1; i <= 10; i++)
    {
      guint8 sample[64 * 80];
      fte3600_auth_test_image (sample, 64, 80, FTE3600_AUTH_TEST_SEED, i);
      g_assert_true (fte3600_auth_enrollment_add (enrollment, sample, sizeof sample, 64, &count, &error));
      g_assert_no_error (error);
    }
  g_assert_cmpuint (count, ==, FTE3600_AUTH_ENROLL_SAMPLES);
  g_autoptr(Fte3600AuthTemplate) print = fte3600_auth_enrollment_finish (enrollment, &error);
  g_assert_no_error (error);
  g_assert_nonnull (print);

  g_autoptr(Fte3600AuthProbe) impostor_probe =
    fte3600_auth_probe_new (&identity, impostor, sizeof impostor, 64, &error);
  g_assert_no_error (error);
  Fte3600AuthMatch match = { 0 };
  g_assert_true (fte3600_auth_match (impostor_probe, print, &match, &error));
  g_assert_no_error (error);
  g_assert_false (match.matched);

  g_autoptr(Fte3600AuthTemplate) clean_print = enroll (&identity, FTE3600_AUTH_TEST_SEED);
  guint8 warped[64 * 80];
  for (guint y = 0; y < 80; y++)
    for (guint x = 0; x < 64; x++)
      {
        const gint sx = CLAMP ((gint) floor (32.0 + (x - 32.0) / 1.220 + 0.5), 0, 63);
        const gint sy = CLAMP ((gint) floor (40.0 + (y - 40.0) / 1.220 + 0.5), 0, 79);
        warped[y * 64 + x] = anchor[sy * 64 + sx];
      }
  g_autoptr(Fte3600AuthProbe) warped_probe =
    fte3600_auth_probe_new (&identity, warped, sizeof warped, 64, &error);
  g_assert_no_error (error);
  g_assert_true (fte3600_auth_match (warped_probe, clean_print, &match, &error));
  g_assert_no_error (error);
  g_assert_cmpuint (match.mutual_matches, >=, 6u);
  g_assert_false (match.matched);
}

static void
test_golden_030 (void)
{
  /* Deterministic 0x1002u 12-sample CLAHE template round-trip check. */
  Fte3600AuthIdentity identity = identity_for (&identities[2]);

  g_autoptr(Fte3600AuthTemplate) print = enroll (&identity, FTE3600_AUTH_TEST_SEED);
  g_autoptr(GBytes) bytes = fte3600_auth_template_encode (print);
  g_assert_nonnull (bytes);
  g_assert_cmpuint (FTE3600_AUTH_ENROLL_SAMPLES, ==, 12u);
  g_assert_cmpuint (g_bytes_get_size (bytes), <=, FTE3600_AUTH_MAX_TEMPLATE_BYTES);
  g_autoptr(GError) error = NULL;
  g_autoptr(Fte3600AuthTemplate) loaded = fte3600_auth_template_decode (bytes, &error);
  g_assert_no_error (error);
  g_assert_nonnull (loaded);
  g_autoptr(GBytes) saved = fte3600_auth_template_encode (loaded);
  g_assert_true (g_bytes_equal (bytes, saved));
}

int
main (int argc, char **argv)
{
  g_test_init (&argc, &argv, NULL);
  for (guint i = 0; i < G_N_ELEMENTS (identities); i++)
    {
      g_autofree gchar *path = g_strdup_printf ("/auth/profile/%04x", identities[i].chip);
      g_test_add_data_func (path, &identities[i], test_profile);
    }
  g_test_add_func ("/auth/rejects", test_rejects);
  g_test_add_func ("/auth/identity", test_identity);
  g_test_add_func ("/auth/persist-identify", test_persist_identify);
  g_test_add_func ("/auth/best-match", test_best_match);
  g_test_add_func ("/auth/strip-coverage", test_strip_coverage);
  g_test_add_func ("/auth/codec", test_codec);
  g_test_add_func ("/auth/golden-0.3.0", test_golden_030);
  g_test_add_func ("/auth/clahe-determinism-blank", test_clahe_determinism_and_blank_guard);
  g_test_add_func ("/auth/clahe-low-contrast", test_clahe_low_contrast_extraction);
  g_test_add_func ("/auth/processing-version-rejected", test_processing_version_rejected);
  g_test_add_func ("/auth/two-tier-enrollment", test_two_tier_enrollment_gate);
  g_test_add_func ("/auth/feature-multisets", test_feature_multisets);
  g_test_add_func ("/auth/cancellation", test_cancellation);
  if (g_test_perf ())
    g_test_add_func ("/auth/performance", test_performance);
  return g_test_run ();
}

<?php

declare(strict_types=1);

/**
 * Seed one PUBLIC, EDITABLE feature service from a GeoJSON fixture, so the
 * headless-QGIS round-trip (roundtrip.py) can add it, edit it and commit
 * without any sign-in. Prints the service slug on stdout.
 *
 * Usage:  GEOI_DATA_DIR=/tmp/x php seed.php point.geojson edittest
 */

error_reporting(E_ALL);
ini_set('display_errors', 'stderr');

require __DIR__ . '/../../../src/platform/lib/bootstrap.php';

$fixture = $argv[1] ?? (__DIR__ . '/point.geojson');
$name    = $argv[2] ?? 'edittest';
if (!is_file($fixture)) {
    fwrite(STDERR, "fixture not found: {$fixture}\n");
    exit(1);
}

try {
    $svc  = Ingest::ingest($fixture, $name . '.geojson', ['name' => $name]);
    $slug = (string)$svc['name'];
    ServiceRegistry::ensure($slug);
    ServiceRegistry::setVisibility($slug, 'public');
    Catalog::setEditable($slug, true);
    echo $slug . "\n";
} catch (Throwable $e) {
    fwrite(STDERR, 'seed failed: ' . $e->getMessage() . "\n");
    exit(1);
}

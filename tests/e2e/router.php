<?php

declare(strict_types=1);

/**
 * `php -S` router for the geoi platform — TEST / E2E ONLY.
 *
 * Mirrors the production .htaccess rewrites just enough to drive the REST,
 * hub and auth controllers over real HTTP, so a headless QGIS can talk to a
 * live platform. NOT for production use.
 */

$platform = dirname(__DIR__, 3) . '/src/platform';
$uri  = (string)parse_url($_SERVER['REQUEST_URI'] ?? '/', PHP_URL_PATH);
$path = preg_replace('#^/platform/?#', '', $uri) ?? '';
$path = ltrim($path, '/');
$section = strtok($path, '/') ?: '';

// WFS shares the /rest path space and must win before the generic rest rule.
if (preg_match('#^rest/services/[^/]+/WFSServer#', $path) === 1) {
    require $platform . '/api/wfs.php';
    return true;
}

$controllers = [
    'rest'    => 'api/rest.php',
    'hub'     => 'api/hub.php',
    'auth'    => 'api/auth.php',
    'sharing' => 'api/sharing.php',
    'admin'   => 'api/admin.php',
];
if (isset($controllers[$section])) {
    require $platform . '/' . $controllers[$section];
    return true;
}

http_response_code(404);
header('Content-Type: application/json');
echo json_encode(['error' => ['code' => 404, 'message' => 'Unknown endpoint']]);
return true;

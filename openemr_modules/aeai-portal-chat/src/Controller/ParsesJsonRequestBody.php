<?php

namespace AeaiPortalChat\Controller;

use Symfony\Component\HttpFoundation\JsonResponse;

/**
 * Shared by every module-added write route controller
 * (`AssessmentDraftController`, `AppointmentCancelController`,
 * `AppointmentBookController`). `HttpRestRequest::getRequestBodyJSON()` calls
 * `->getContents()` on a raw PHP resource in this OpenEMR version and fatals; every
 * core route reads the body this way instead (see e.g.
 * `apis/routes/_rest_routes_standard.inc.php`). A missing/empty body is treated as
 * an empty object (nothing to validate against, `requireComplete=false`-style
 * callers), not an error; anything present that fails to parse is a 400, not a
 * silently-dropped write.
 */
trait ParsesJsonRequestBody
{
    /**
     * @return array|JsonResponse Decoded body, or a 400 if it isn't valid JSON.
     */
    private function parseJsonBody(): array|JsonResponse
    {
        $raw = file_get_contents('php://input');
        if ($raw === false || trim($raw) === '') {
            return [];
        }
        $decoded = json_decode($raw, true);
        if (json_last_error() !== JSON_ERROR_NONE || !is_array($decoded)) {
            return new JsonResponse(['error' => 'request body must be a JSON object'], 400);
        }
        return $decoded;
    }
}

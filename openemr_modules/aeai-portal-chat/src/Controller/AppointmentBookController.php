<?php

namespace AeaiPortalChat\Controller;

use AeaiPortalChat\Service\AppointmentBookService;
use OpenEMR\Common\Http\HttpRestRequest;
use OpenEMR\Events\RestApiExtend\RestApiCreateEvent;
use OpenEMR\Events\RestApiExtend\RestApiScopeEvent;
use Symfony\Component\EventDispatcher\EventDispatcherInterface;
use Symfony\Component\HttpFoundation\JsonResponse;

/**
 * Adds the patient-writable "book a new appointment" action (TICK-040) -- see
 * `AppointmentBookService`'s own doc comment for why the Standard API route
 * (`POST /api/patient/:pid/appointment`) is structurally unreachable for a genuine
 * patient token and this module route exists instead. Mirrors
 * `AppointmentCancelController`'s own registration pattern exactly: same
 * `RestApiExtend` events, same Portal route family, no core file touched.
 */
class AppointmentBookController
{
    use ParsesJsonRequestBody;

    public function subscribeToEvents(EventDispatcherInterface $eventDispatcher): void
    {
        $eventDispatcher->addListener(RestApiScopeEvent::EVENT_TYPE_GET_SUPPORTED_SCOPES, $this->addScopes(...));
        $eventDispatcher->addListener(RestApiCreateEvent::EVENT_HANDLE, $this->addRoutes(...));
    }

    public function addScopes(RestApiScopeEvent $event): RestApiScopeEvent
    {
        $event->addScope('patient', 'appointment', 'c');
        return $event;
    }

    public function addRoutes(RestApiCreateEvent $event): RestApiCreateEvent
    {
        $event->addToPortalRouteMap(
            'POST /portal/patient/appointment',
            function (HttpRestRequest $request) {
                $body = $this->parseJsonBody();
                if ($body instanceof JsonResponse) {
                    return $body;
                }
                return (new AppointmentBookService())->book($request->getPatientUUIDString(), $body);
            }
        );
        return $event;
    }
}

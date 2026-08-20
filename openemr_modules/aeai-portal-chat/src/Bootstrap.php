<?php

namespace AeaiPortalChat;

use AeaiPortalChat\Controller\AssessmentDraftController;
use AeaiPortalChat\Controller\PortalChatController;
use Symfony\Component\EventDispatcher\EventDispatcherInterface;

/**
 * Wires the portal-chat and assessment-draft listeners into OpenEMR's event
 * dispatcher. This same dispatcher is used both for portal page renders
 * (RenderEvent, via interface/globals.php during a normal page load) and for REST
 * API requests (RestApiCreateEvent/RestApiScopeEvent, via the same globals.php path
 * during apis/dispatch.php -- see interface/globals.php's ModulesApplication
 * construction, which wraps the API kernel's own event dispatcher).
 */
class Bootstrap
{
    public function __construct(private readonly EventDispatcherInterface $eventDispatcher)
    {
    }

    public function subscribeToEvents(): void
    {
        (new PortalChatController())->subscribeToEvents($this->eventDispatcher);
        (new AssessmentDraftController())->subscribeToEvents($this->eventDispatcher);
    }
}

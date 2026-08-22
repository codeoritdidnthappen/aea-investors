<?php

/**
 * TICK-054: renders PortalChatController's two echoes with the real PHP the
 * OpenEMR container runs, so the live verification harness embeds byte-for-byte
 * what the module will emit once merged rather than a hand-copied approximation.
 *
 * The controller only ever names the OpenEMR/Symfony event classes in type hints,
 * so minimal stubs are enough to execute it outside OpenEMR's autoloader.
 *
 * Run:
 *   docker cp openemr_modules/aeai-portal-chat/src/Controller/PortalChatController.php \
 *     local-openemr-1:/tmp/PortalChatController.php
 *   docker cp evidence/TICK-054/render_panel_probe.php local-openemr-1:/tmp/probe.php
 *   docker exec local-openemr-1 php /tmp/probe.php
 */

namespace OpenEMR\Events\PatientPortal {
    class RenderEvent
    {
        public const EVENT_SECTION_RENDER_POST = 'section.render.post';
        public const EVENT_DASHBOARD_INJECT_CARD = 'dashboard.inject.card';
    }
}

namespace Symfony\Component\EventDispatcher {
    interface EventDispatcherInterface
    {
        public function addListener(string $eventName, callable $listener): void;
    }

    class GenericEvent
    {
    }
}

namespace {
    require '/tmp/PortalChatController.php';

    $controller = new AeaiPortalChat\Controller\PortalChatController();
    $event = new Symfony\Component\EventDispatcher\GenericEvent();

    echo "<!--TILE-->\n";
    $controller->renderDashboardTile($event);
    echo "\n<!--PANEL-->\n";
    $controller->render($event);
    echo "\n";
}

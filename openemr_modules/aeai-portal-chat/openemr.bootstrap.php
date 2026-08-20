<?php

/**
 * Bootstrap for the AEA Investors AI portal chat module (TICK-012).
 *
 * Subscribes to OpenEMR\Events\PatientPortal\RenderEvent::EVENT_SECTION_RENDER_POST,
 * the hook selected by TICK-002 (evidence/TICK-002/PORTAL_HOOK_EVIDENCE.md). That event
 * is dispatched only from portal/home.php, which never renders for a logged-out
 * visitor, so an unauthenticated user cannot see or launch this entry (FR-1) by
 * construction — this module adds no route and no check that could be forgotten.
 *
 * Registration: a `modules` row with mod_directory='aeai-portal-chat', type=0
 * (custom, not Laminas), mod_active=1, enabled via Admin > Modules > Manage Modules
 * (see deploy/local/README.md).
 *
 * @var \OpenEMR\Core\ModulesClassLoader $classLoader Injected by the OpenEMR module loader
 * @var \Symfony\Component\EventDispatcher\EventDispatcherInterface $eventDispatcher Injected by the OpenEMR module loader
 */

use AeaiPortalChat\Bootstrap;

$classLoader->registerNamespaceIfNotExists('AeaiPortalChat\\', __DIR__ . DIRECTORY_SEPARATOR . 'src');

(new Bootstrap($eventDispatcher))->subscribeToEvents();

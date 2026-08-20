<?php

namespace AeaiPortalChat;

use AeaiPortalChat\Controller\PortalChatController;
use Symfony\Component\EventDispatcher\EventDispatcherInterface;

/**
 * Wires the portal-chat listener into OpenEMR's event dispatcher.
 */
class Bootstrap
{
    public function __construct(private readonly EventDispatcherInterface $eventDispatcher)
    {
    }

    public function subscribeToEvents(): void
    {
        (new PortalChatController())->subscribeToEvents($this->eventDispatcher);
    }
}

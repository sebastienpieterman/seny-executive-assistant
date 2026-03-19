/**
 * Service Worker for Seny Push Notifications - Phase 7 (07-09)
 *
 * Handles:
 * - Push notification events from the server
 * - Notification click events to navigate to the app
 * - Optional notification action buttons
 */

// Service worker version for cache management
const SW_VERSION = '1.0.0';

/**
 * Handle incoming push notifications
 */
self.addEventListener('push', function(event) {
    console.log('[SW] Push notification received');

    // Parse the notification data
    let data = {};
    if (event.data) {
        try {
            data = event.data.json();
        } catch (e) {
            console.error('[SW] Failed to parse push data:', e);
            data = {
                title: 'Seny',
                body: event.data.text() || 'New notification'
            };
        }
    }

    // Notification options (icons omitted - not required)
    const options = {
        body: data.body || 'Notification from Seny',
        vibrate: [100, 50, 100],
        tag: data.type || 'general',  // Group by type
        renotify: true,  // Vibrate even if replacing a notification
        data: {
            url: data.url || '/',
            type: data.type || 'general',
            timestamp: data.timestamp || new Date().toISOString()
        }
    };

    // Add action buttons for certain notification types
    if (data.type === 'timer') {
        options.actions = [
            { action: 'dismiss', title: 'Dismiss' }
        ];
        options.requireInteraction = true;  // Keep visible until dismissed
    } else if (data.type === 'alarm') {
        options.actions = [
            { action: 'snooze', title: 'Snooze 5m' },
            { action: 'dismiss', title: 'Dismiss' }
        ];
        options.requireInteraction = true;
    } else if (data.type === 'task_reminder') {
        options.actions = [
            { action: 'view', title: 'View Task' },
            { action: 'dismiss', title: 'Dismiss' }
        ];
    }

    // If custom actions were provided, use those instead
    if (data.actions && Array.isArray(data.actions)) {
        options.actions = data.actions;
    }

    // Show the notification
    console.log('[SW] Showing notification:', data.title, options);
    event.waitUntil(
        self.registration.showNotification(data.title || 'Seny', options)
            .then(() => console.log('[SW] Notification shown successfully'))
            .catch(err => console.error('[SW] Failed to show notification:', err))
    );
});


/**
 * Handle notification clicks
 */
self.addEventListener('notificationclick', function(event) {
    console.log('[SW] Notification clicked:', event.action);

    // Close the notification
    event.notification.close();

    // Handle actions
    if (event.action === 'dismiss') {
        // Just close, no navigation
        return;
    }

    if (event.action === 'snooze') {
        // TODO: Could implement snooze by calling an API endpoint
        console.log('[SW] Snooze requested');
        return;
    }

    // Get the URL to open
    const url = event.notification.data?.url || '/';
    const fullUrl = new URL(url, self.location.origin).href;

    // Try to focus an existing window or open a new one
    event.waitUntil(
        clients.matchAll({ type: 'window', includeUncontrolled: true })
            .then(function(clientList) {
                // Check if there's already a window open
                for (let client of clientList) {
                    if (client.url.startsWith(self.location.origin) && 'focus' in client) {
                        // Focus the existing window and navigate
                        return client.focus().then(function(focusedClient) {
                            if (focusedClient.navigate) {
                                return focusedClient.navigate(fullUrl);
                            }
                            return focusedClient;
                        });
                    }
                }
                // No existing window, open a new one
                return clients.openWindow(fullUrl);
            })
    );
});


/**
 * Handle notification close (user dismissed)
 */
self.addEventListener('notificationclose', function(event) {
    console.log('[SW] Notification closed');
    // Could track dismissal analytics here
});


/**
 * Service worker installation
 */
self.addEventListener('install', function(event) {
    console.log('[SW] Service worker installed, version:', SW_VERSION);
    // Skip waiting to activate immediately
    self.skipWaiting();
});


/**
 * Service worker activation
 */
self.addEventListener('activate', function(event) {
    console.log('[SW] Service worker activated, version:', SW_VERSION);
    // Take control of all clients immediately
    event.waitUntil(clients.claim());
});

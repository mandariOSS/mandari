/**
 * Real-time Notifications Module
 *
 * Uses Server-Sent Events (SSE) for real-time notification updates.
 * Falls back to polling if SSE is not supported or connection fails.
 */

(function() {
    'use strict';

    // Configuration
    const CONFIG = {
        pollInterval: 10000,  // 10 seconds for polling fallback
        sseReconnectDelay: 5000,  // 5 seconds before reconnecting SSE
        maxReconnectAttempts: 10,
        toastDuration: 5000,  // 5 seconds for toast notifications
    };

    // State
    let eventSource = null;
    let reconnectAttempts = 0;
    let pollingInterval = null;
    let lastCount = 0;
    let orgSlug = null;

    /**
     * Initialize the notification system
     */
    function init() {
        // Get org slug from data attribute or URL
        const container = document.querySelector('[data-org-slug]');
        if (container) {
            orgSlug = container.dataset.orgSlug;
        } else {
            // Try to extract from URL
            const match = window.location.pathname.match(/\/work\/([^\/]+)/);
            if (match) {
                orgSlug = match[1];
            }
        }

        if (!orgSlug) {
            console.log('Notifications: No org slug found, skipping initialization');
            return;
        }

        // Try SSE first, fall back to polling
        if (window.EventSource) {
            connectSSE();
        } else {
            startPolling();
        }

        // Listen for visibility changes to reconnect when page becomes visible
        document.addEventListener('visibilitychange', handleVisibilityChange);
    }

    /**
     * Connect to SSE endpoint
     */
    function connectSSE() {
        if (eventSource) {
            eventSource.close();
        }

        const streamUrl = `/work/${orgSlug}/notifications/stream/`;
        console.log('Notifications: Connecting to SSE at', streamUrl);

        eventSource = new EventSource(streamUrl);

        eventSource.onopen = function() {
            console.log('Notifications: SSE connection established');
            reconnectAttempts = 0;
            stopPolling();
        };

        eventSource.onmessage = function(event) {
            try {
                const data = JSON.parse(event.data);
                handleNotificationUpdate(data);
            } catch (e) {
                console.error('Notifications: Failed to parse SSE data', e);
            }
        };

        eventSource.onerror = function(error) {
            console.warn('Notifications: SSE error, attempting reconnect...', error);
            eventSource.close();

            if (reconnectAttempts < CONFIG.maxReconnectAttempts) {
                reconnectAttempts++;
                setTimeout(connectSSE, CONFIG.sseReconnectDelay);
            } else {
                console.log('Notifications: Max reconnect attempts reached, falling back to polling');
                startPolling();
            }
        };
    }

    /**
     * Start polling fallback
     */
    function startPolling() {
        if (pollingInterval) return;

        console.log('Notifications: Starting polling');
        pollingInterval = setInterval(poll, CONFIG.pollInterval);
        poll(); // Initial poll
    }

    /**
     * Stop polling
     */
    function stopPolling() {
        if (pollingInterval) {
            clearInterval(pollingInterval);
            pollingInterval = null;
        }
    }

    /**
     * Poll for notifications
     */
    async function poll() {
        try {
            const response = await fetch(`/work/${orgSlug}/notifications/latest/`);
            if (response.ok) {
                const data = await response.json();
                handleNotificationUpdate({
                    type: 'update',
                    count: data.count,
                    notifications: data.notifications,
                });
            }
        } catch (error) {
            console.error('Notifications: Polling error', error);
        }
    }

    /**
     * Handle notification update from SSE or polling
     */
    function handleNotificationUpdate(data) {
        const { type, count, notification, notifications } = data;

        // Update badge count
        updateBadgeCount(count);

        // Show toast for new notifications
        if (type === 'update' && count > lastCount) {
            if (notification) {
                showToast(notification);
            } else if (notifications && notifications.length > 0) {
                showToast(notifications[0]);
            }
        }

        lastCount = count;

        // Dispatch custom event for other components to react
        window.dispatchEvent(new CustomEvent('notification:update', {
            detail: { count, notification, notifications }
        }));
    }

    /**
     * Update notification badge count in UI
     */
    function updateBadgeCount(count) {
        // Update all elements with notification count
        const badges = document.querySelectorAll('[data-notification-count]');
        badges.forEach(badge => {
            badge.textContent = count > 99 ? '99+' : count;
            badge.style.display = count > 0 ? '' : 'none';
        });

        // Update title if there are unread notifications
        if (count > 0 && !document.title.startsWith('(')) {
            document.title = `(${count}) ${document.title}`;
        } else if (count === 0 && document.title.startsWith('(')) {
            document.title = document.title.replace(/^\(\d+\)\s*/, '');
        }
    }

    /**
     * Show toast notification
     */
    function showToast(notification) {
        // Check if toast container exists, create if not
        let container = document.getElementById('notification-toast-container');
        if (!container) {
            container = document.createElement('div');
            container.id = 'notification-toast-container';
            container.className = 'fixed top-4 right-4 z-50 space-y-2';
            document.body.appendChild(container);
        }

        // Create toast element
        const toast = document.createElement('div');
        toast.className = 'notification-toast bg-white dark:bg-gray-800 rounded-lg shadow-lg border border-gray-200 dark:border-gray-700 p-4 max-w-sm transform transition-all duration-300 translate-x-full opacity-0';
        toast.innerHTML = `
            <div class="flex items-start gap-3">
                <div class="flex-shrink-0 w-8 h-8 rounded-full bg-primary-100 dark:bg-primary-900/30 flex items-center justify-center">
                    <i data-lucide="${notification.icon || 'bell'}" class="w-4 h-4 text-primary-600 dark:text-primary-400"></i>
                </div>
                <div class="flex-1 min-w-0">
                    <p class="text-sm font-medium text-gray-900 dark:text-white">${escapeHtml(notification.title)}</p>
                    <p class="text-xs text-gray-500 dark:text-gray-400 mt-1 line-clamp-2">${escapeHtml(notification.message)}</p>
                </div>
                <button class="flex-shrink-0 text-gray-400 hover:text-gray-500" onclick="this.closest('.notification-toast').remove()">
                    <i data-lucide="x" class="w-4 h-4"></i>
                </button>
            </div>
        `;

        // Add click handler for the toast body
        if (notification.link) {
            toast.style.cursor = 'pointer';
            toast.addEventListener('click', (e) => {
                if (!e.target.closest('button')) {
                    window.location.href = notification.link;
                }
            });
        }

        container.appendChild(toast);

        // Trigger Lucide icon replacement
        if (window.lucide) {
            window.lucide.createIcons({ icons: window.lucide.icons, node: toast });
        }

        // Animate in
        requestAnimationFrame(() => {
            toast.classList.remove('translate-x-full', 'opacity-0');
        });

        // Auto-remove after duration
        setTimeout(() => {
            toast.classList.add('translate-x-full', 'opacity-0');
            setTimeout(() => toast.remove(), 300);
        }, CONFIG.toastDuration);
    }

    /**
     * Handle visibility change
     */
    function handleVisibilityChange() {
        if (document.visibilityState === 'visible') {
            // Reconnect SSE or poll immediately when page becomes visible
            if (eventSource && eventSource.readyState === EventSource.CLOSED) {
                connectSSE();
            } else if (!eventSource && pollingInterval) {
                poll();
            }
        }
    }

    /**
     * Escape HTML to prevent XSS
     */
    function escapeHtml(text) {
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    }

    /**
     * Public API
     */
    window.MandariNotifications = {
        init: init,
        poll: poll,
        reconnect: connectSSE,
        getCount: () => lastCount,
    };

    // Auto-initialize when DOM is ready
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();

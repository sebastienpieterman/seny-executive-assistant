import { useState, useEffect } from "react";
import { api } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { Bell, Trash2 } from "lucide-react";
import { toast } from "sonner";

interface Device {
  id: number;
  device_name: string;
  created_at: string;
  last_used_at: string | null;
}

export function NotificationsTab() {
  const [permission, setPermission] = useState<NotificationPermission>(
    typeof Notification !== "undefined" ? Notification.permission : "default"
  );
  const [subscribed, setSubscribed] = useState(false);
  const [devices, setDevices] = useState<Device[]>([]);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    checkSubscription();
    loadDevices();
  }, []);

  async function checkSubscription() {
    try {
      const reg = await navigator.serviceWorker.ready;
      const sub = await reg.pushManager.getSubscription();
      setSubscribed(sub !== null);
    } catch {
      setSubscribed(false);
    }
  }

  async function loadDevices() {
    try {
      const data = await api.get<{ devices: Device[]; total: number }>(
        "/api/notifications/devices"
      );
      setDevices(data.devices);
    } catch {
      // Notifications may not be configured
    }
  }

  async function handleToggle(enabled: boolean) {
    setSubscribed(enabled);
    if (enabled) {
      await subscribePush();
    } else {
      await unsubscribePush();
    }
  }

  async function subscribePush() {
    setLoading(true);
    try {
      if (typeof Notification === "undefined") {
        toast.error("Push notifications not supported in this browser");
        setSubscribed(false);
        return;
      }

      const perm = await Notification.requestPermission();
      setPermission(perm);

      if (perm !== "granted") {
        toast.error("Notification permission denied");
        setSubscribed(false);
        return;
      }

      // Get VAPID key
      const { public_key } = await api.get<{ public_key: string }>(
        "/api/notifications/vapid-public-key"
      );

      // Subscribe with service worker
      const reg = await navigator.serviceWorker.ready;
      const sub = await reg.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: urlBase64ToUint8Array(public_key),
      });

      const subJson = sub.toJSON();
      await api.post("/api/notifications/subscribe", {
        endpoint: subJson.endpoint,
        keys: subJson.keys,
      });

      toast.success("Push notifications enabled");
      await checkSubscription();
      await loadDevices();
    } catch (err) {
      await checkSubscription();
      toast.error("Failed to enable push notifications");
      console.error(err);
    } finally {
      setLoading(false);
    }
  }

  async function unsubscribePush() {
    setLoading(true);
    try {
      const reg = await navigator.serviceWorker.ready;
      const sub = await reg.pushManager.getSubscription();
      if (sub) {
        await api.delete(
          `/api/notifications/subscribe?endpoint=${encodeURIComponent(sub.endpoint)}`
        );
        await sub.unsubscribe();
      }
      toast.success("Push notifications disabled");
      await checkSubscription();
      await loadDevices();
    } catch {
      await checkSubscription();
      toast.error("Failed to disable push notifications");
    } finally {
      setLoading(false);
    }
  }

  async function handleTestNotification() {
    try {
      const result = await api.post<{ sent: number; message: string }>(
        "/api/notifications/test"
      );
      if (result.sent > 0) {
        toast.success(result.message);
      } else {
        toast.error(result.message);
      }
    } catch {
      toast.error("Failed to send test notification");
    }
  }

  async function removeDevice(deviceId: number) {
    try {
      await api.delete(`/api/notifications/devices/${deviceId}`);
      toast.success("Device removed");
      loadDevices();
    } catch {
      toast.error("Failed to remove device");
    }
  }

  return (
    <div className="space-y-8">
      <div>
        <h3 className="text-lg font-semibold">Notifications</h3>
        <p className="text-sm text-muted-foreground">
          Manage push notification preferences.
        </p>
      </div>

      {/* Push toggle */}
      <div className="flex items-center justify-between rounded-lg border border-border bg-card p-4">
        <div className="flex items-center gap-3">
          <Bell className="h-5 w-5 text-muted-foreground" />
          <div>
            <Label className="text-sm font-medium">Push Notifications</Label>
            <p className="text-xs text-muted-foreground">
              {permission === "denied"
                ? "Blocked by browser. Update in browser settings."
                : "Receive alerts for tasks, reminders, and messages."}
            </p>
          </div>
        </div>
        <Switch
          checked={subscribed}
          onCheckedChange={handleToggle}
          disabled={loading || permission === "denied"}
        />
      </div>

      {/* Test button */}
      {subscribed && (
        <Button variant="outline" size="sm" onClick={handleTestNotification}>
          Send Test Notification
        </Button>
      )}

      {/* Devices */}
      {devices.length > 0 && (
        <div className="space-y-3">
          <Label>Subscribed Devices</Label>
          {devices.map((d) => (
            <div
              key={d.id}
              className="flex items-center justify-between rounded-md border border-border p-3"
            >
              <div>
                <span className="text-sm">{d.device_name}</span>
                <p className="text-xs text-muted-foreground">
                  Added {new Date(d.created_at).toLocaleDateString()}
                </p>
              </div>
              <Button
                variant="ghost"
                size="sm"
                onClick={() => removeDevice(d.id)}
              >
                <Trash2 className="h-4 w-4" />
              </Button>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function urlBase64ToUint8Array(base64String: string): Uint8Array<ArrayBuffer> {
  const padding = "=".repeat((4 - (base64String.length % 4)) % 4);
  const base64 = (base64String + padding).replace(/-/g, "+").replace(/_/g, "/");
  const rawData = window.atob(base64);
  const outputArray = new Uint8Array(rawData.length);
  for (let i = 0; i < rawData.length; ++i) {
    outputArray[i] = rawData.charCodeAt(i);
  }
  return outputArray;
}

import type * as ReactTypes from "react";

type Component<P = Record<string, unknown>> = ReactTypes.ComponentType<P & { children?: ReactTypes.ReactNode }>;

export interface HermesPluginSDK {
  React: typeof ReactTypes;
  authedFetch: (url: string, init?: RequestInit) => Promise<Response>;
  hooks: { useToast: (duration?: number) => { showToast: (message: string, type: "error" | "success") => void; toast: { message: string; type: "error" | "success" } | null } };
  components: {
    Button: Component<ReactTypes.ButtonHTMLAttributes<HTMLButtonElement> & { size?: "sm" | "default" | "icon" | "xs"; ghost?: boolean; outlined?: boolean; destructive?: boolean; prefix?: ReactTypes.ReactNode }>;
    Input: Component<ReactTypes.InputHTMLAttributes<HTMLInputElement>>;
    Badge: Component<ReactTypes.HTMLAttributes<HTMLSpanElement> & { tone?: "default" | "destructive" | "outline" | "secondary" | "success" | "warning" }>;
    Select: Component<{ value?: string; onValueChange?: (value: string) => void; className?: string; id?: string; placeholder?: string; disabled?: boolean }>;
    SelectOption: Component<{ value: string }>;
    Dialog: Component<{ open: boolean; onOpenChange: (open: boolean) => void }>;
    DialogContent: Component<{ className?: string }>;
    DialogHeader: Component;
    DialogTitle: Component;
    DialogDescription: Component;
    ConfirmDialog: Component<{ open: boolean; title: string; description?: string; confirmLabel?: string; destructive?: boolean; loading?: boolean; onCancel: () => void; onConfirm: () => void }>;
    Toast: Component<{ toast: { message: string; type: "error" | "success" } | null }>;
  };
  utils: { timeAgo: (unixSeconds: number) => string; cn: (...classes: unknown[]) => string };
}

declare global {
  interface Window {
    __HERMES_PLUGIN_SDK__: HermesPluginSDK;
    __HERMES_PLUGINS__: { register: (name: string, component: ReactTypes.ComponentType) => void };
  }
}

export const SDK = window.__HERMES_PLUGIN_SDK__;
export const React = SDK.React;

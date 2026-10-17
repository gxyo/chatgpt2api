"use client";

import { useEffect, useRef } from "react";
import { BookOpen, KeyRound, LoaderCircle, SlidersHorizontal, Tags } from "lucide-react";

import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { useAuthGuard } from "@/lib/use-auth-guard";

import { ApiDocsCard } from "./components/api-docs-card";
import { ConfigCard } from "./components/config-card";
import { ModelPricingCard } from "./components/model-pricing-card";
import { SettingsHeader } from "./components/settings-header";
import { UserKeysCard } from "./components/user-keys-card";
import { useSettingsStore } from "./store";

const settingsTabs = [
  { value: "basic", title: "基础配置", icon: SlidersHorizontal },
  { value: "model-pricing", title: "模型与定价", icon: Tags },
  { value: "keys", title: "用户密钥", icon: KeyRound },
  { value: "api-docs", title: "接口接入", icon: BookOpen },
];

function SettingsDataController() {
  const didLoadRef = useRef(false);
  const initialize = useSettingsStore((state) => state.initialize);

  useEffect(() => {
    if (didLoadRef.current) {
      return;
    }
    didLoadRef.current = true;
    void initialize();
  }, [initialize]);

  return null;
}

function SettingsPageContent() {
  return (
    <>
      <SettingsDataController />
      <SettingsHeader />
      <Tabs defaultValue="basic" className="space-y-4">
        <div className="sticky top-[4.5rem] z-20 -mx-1 overflow-x-auto px-1 py-1 lg:top-3">
          <TabsList className="min-w-max justify-start rounded-full border-border bg-card/95 p-1 shadow-[0_1px_2px_rgba(15,23,42,0.04)] backdrop-blur">
            {settingsTabs.map((tab) => {
              const Icon = tab.icon;
              return (
                <TabsTrigger key={tab.value} value={tab.value} className="gap-2 rounded-full px-4">
                  <Icon className="size-4" />
                  {tab.title}
                </TabsTrigger>
              );
            })}
          </TabsList>
        </div>
        <TabsContent value="basic">
          <ConfigCard />
        </TabsContent>
        <TabsContent value="model-pricing">
          <ModelPricingCard />
        </TabsContent>
        <TabsContent value="keys">
          <UserKeysCard />
        </TabsContent>
        <TabsContent value="api-docs">
          <ApiDocsCard />
        </TabsContent>
      </Tabs>
    </>
  );
}

export default function SettingsPage() {
  const { isCheckingAuth, session } = useAuthGuard(["admin"]);

  if (isCheckingAuth || !session || session.role !== "admin") {
    return (
      <div className="flex min-h-[40vh] items-center justify-center">
        <LoaderCircle className="size-5 animate-spin text-neutral-400" />
      </div>
    );
  }

  return <SettingsPageContent />;
}

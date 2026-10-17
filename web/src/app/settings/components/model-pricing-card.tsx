"use client";

import { useState } from "react";
import { CircleDollarSign, Cpu, LoaderCircle, Plus, Save, Trash2 } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";

import { useSettingsStore } from "../store";

// 与日志里的 detail.model 精确匹配，不做大小写或前缀归一化。
const PRESET_MODELS = [
  "gpt-image-2",
  "gpt-image-2.5",
  "gpt-image-2.5-sunburst",
  "gpt-image-2.5-flare",
  "codex-gpt-image-2",
  "plus-codex-gpt-image-2",
  "team-codex-gpt-image-2",
  "pro-codex-gpt-image-2",
  "auto",
];

const THINKING_EFFORT_OPTIONS = [
  { value: "auto", label: "auto（默认）" },
  { value: "standard", label: "standard" },
  { value: "extended", label: "extended" },
  { value: "max", label: "max" },
];

export function ModelPricingCard() {
  const [customModel, setCustomModel] = useState("");
  const config = useSettingsStore((state) => state.config);
  const isLoadingConfig = useSettingsStore((state) => state.isLoadingConfig);
  const isSavingConfig = useSettingsStore((state) => state.isSavingConfig);
  const setDefaultUpstreamModelName = useSettingsStore((state) => state.setDefaultUpstreamModelName);
  const setDefaultThinkingEffort = useSettingsStore((state) => state.setDefaultThinkingEffort);
  const setModelPrice = useSettingsStore((state) => state.setModelPrice);
  const addPricingModel = useSettingsStore((state) => state.addPricingModel);
  const removePricingModel = useSettingsStore((state) => state.removePricingModel);
  const saveConfig = useSettingsStore((state) => state.saveConfig);

  const pricing = config?.model_pricing || {};
  const hiddenModels = config?.hidden_pricing_models || [];
  const customModels = Object.keys(pricing).filter((model) => !PRESET_MODELS.includes(model));
  // 删除按钮对每一行都可用：预设模型删掉后记入隐藏列表，重新添加时恢复。
  const rows = [
    ...PRESET_MODELS.filter((model) => !hiddenModels.includes(model)),
    ...customModels,
  ];

  const handleAddModel = () => {
    const model = customModel.trim();
    if (!model) return;
    addPricingModel(model);
    setCustomModel("");
  };

  if (isLoadingConfig) {
    return (
      <Card>
        <CardContent className="flex items-center justify-center p-10">
          <LoaderCircle className="size-5 animate-spin text-neutral-400" />
        </CardContent>
      </Card>
    );
  }

  return (
    <Card>
      <CardContent className="space-y-5 p-6 pb-24 md:pr-28 md:pb-6">
        <div className="rounded-xl border border-neutral-200 bg-neutral-50 px-4 py-3 text-sm leading-6 text-neutral-600">
          默认上游模型决定生图请求实际发往官网的模型；模型定价按「每张成功生成的图片」计费（一次请求返回多张图按张数计费，失败与安全拦截不计费），用于统计页估算收入。
        </div>

        <div className="grid gap-4 md:grid-cols-2">
          <div className="space-y-2">
            <label className="flex items-center gap-2 text-sm text-neutral-700">
              <Cpu className="size-4 text-neutral-500" />
              默认上游模型
            </label>
            <Input
              value={String(config?.default_upstream_model_name || "")}
              onChange={(event) => setDefaultUpstreamModelName(event.target.value)}
              placeholder="gpt-5-5"
              className="h-10 rounded-xl border-neutral-200 bg-white font-mono text-sm"
            />
            <p className="text-xs leading-5 text-neutral-500">留空时后端回落到 gpt-5-5。</p>
          </div>
          <div className="space-y-2">
            <label className="text-sm text-neutral-700">思考强度</label>
            <Select
              value={String(config?.default_thinking_effort || "auto")}
              onValueChange={setDefaultThinkingEffort}
            >
              <SelectTrigger className="h-10 w-full rounded-xl border-neutral-200 bg-white">
                <SelectValue placeholder="选择思考强度" />
              </SelectTrigger>
              <SelectContent>
                {THINKING_EFFORT_OPTIONS.map((option) => (
                  <SelectItem key={option.value} value={option.value}>
                    {option.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <p className="text-xs leading-5 text-neutral-500">仅对支持思考强度的上游模型生效。</p>
          </div>
        </div>

        <div className="space-y-3">
          <div>
            <div className="flex items-center gap-2 text-sm font-medium text-neutral-800">
              <CircleDollarSign className="size-4 text-neutral-500" />
              模型定价
            </div>
            <p className="mt-1 text-xs leading-5 text-neutral-500">
              单位：人民币 / 张。留空表示未定价（统计页会提示补定价），填 0 表示免费。
            </p>
          </div>

          <div className="overflow-hidden rounded-xl border border-neutral-200 bg-white">
            <div className="grid grid-cols-[minmax(0,1fr)_9rem_2rem] items-center gap-3 border-b border-neutral-100 bg-neutral-50 px-4 py-2 text-xs font-medium text-neutral-500">
              <span>模型</span>
              <span>单价（¥ / 张）</span>
              <span />
            </div>
            <div className="divide-y divide-neutral-100">
              {rows.map((model) => (
                <div key={model} className="grid grid-cols-[minmax(0,1fr)_9rem_2rem] items-center gap-3 px-4 py-2.5">
                  <span className="truncate font-mono text-xs text-neutral-800" title={model}>
                    {model}
                  </span>
                  <div className="flex items-center gap-1.5">
                    <span className="text-xs text-neutral-500">¥</span>
                    <Input
                      type="text"
                      inputMode="decimal"
                      value={pricing[model] === undefined ? "" : String(pricing[model])}
                      onChange={(event) => setModelPrice(model, event.target.value)}
                      placeholder="未定价"
                      aria-label={`${model} 单价`}
                      className="h-8 rounded-lg border-neutral-200 bg-white text-sm tabular-nums"
                    />
                  </div>
                  <Button
                    type="button"
                    variant="ghost"
                    size="icon"
                    className="size-8 rounded-lg text-neutral-400 hover:text-red-600"
                    onClick={() => removePricingModel(model)}
                    aria-label={`删除 ${model} 的定价`}
                    title="删除该模型"
                  >
                    <Trash2 className="size-4" />
                  </Button>
                </div>
              ))}
            </div>
          </div>

          <div className="flex flex-col gap-2 sm:flex-row">
            <Input
              value={customModel}
              onChange={(event) => setCustomModel(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter") {
                  event.preventDefault();
                  handleAddModel();
                }
              }}
              placeholder="添加自定义模型名，例如 gpt-image-3"
              className="h-10 rounded-xl border-neutral-200 bg-white font-mono text-sm"
            />
            <Button
              type="button"
              variant="outline"
              className="h-10 shrink-0 rounded-xl border-neutral-200"
              onClick={handleAddModel}
              disabled={!customModel.trim()}
            >
              <Plus className="size-4" />
              添加模型
            </Button>
          </div>
        </div>
      </CardContent>
      <div className="fixed right-4 bottom-4 z-40 md:top-1/2 md:bottom-auto md:-translate-y-1/2">
        <Button
          className="h-12 rounded-2xl bg-brand px-4 text-white shadow-xl shadow-neutral-950/20 hover:bg-brand/90 focus-visible:ring-2 focus-visible:ring-neutral-900/30 sm:px-5 dark:bg-neutral-100 dark:text-neutral-900 dark:shadow-black/30 dark:hover:bg-white"
          onClick={() => void saveConfig()}
          disabled={isSavingConfig}
          aria-label="保存模型与定价设置"
          title="保存模型与定价设置"
        >
          {isSavingConfig ? <LoaderCircle className="size-4 animate-spin" /> : <Save className="size-4" />}
          保存
        </Button>
      </div>
    </Card>
  );
}

"use client";

import { login } from "@/lib/api";
import { ApiRequestError } from "@/lib/request";
import {
  clearStoredAuthSession,
  getCachedAuthSession,
  getStoredAuthSession,
  setCachedAuthSession,
  setStoredAuthSession,
  type StoredAuthSession,
} from "@/store/auth";

/**
 * 内存缓存的可信时长。窗口内站内跳转直接复用校验结果，不发任何请求；
 * 超窗后仍然先拿旧结果渲染页面（避免跳转卡顿），复检放到后台做。
 * 期间密钥真的被吊销也没关系：任意业务请求收到 401 会被 request.ts 踢回登录页。
 */
const SESSION_TRUST_MS = 5 * 60 * 1000;

let inflightValidation: Promise<StoredAuthSession | null> | null = null;

/** 同步读校验结果，用于首帧渲染；返回 null 表示本次会话还没校验过。 */
export function peekAuthSession() {
  return getCachedAuthSession();
}

async function validate(): Promise<StoredAuthSession | null> {
  const storedSession = await getStoredAuthSession();
  if (!storedSession) {
    setCachedAuthSession(null);
    return null;
  }

  try {
    const data = await login(storedSession.key);
    const nextSession: StoredAuthSession = {
      key: storedSession.key,
      role: data.role,
      subjectId: data.subject_id,
      name: data.name,
    };
    await setStoredAuthSession(nextSession);
    return nextSession;
  } catch (error) {
    if (error instanceof ApiRequestError && error.status !== 401 && error.status !== 403) {
      // 后端抖动或断网：沿用本地会话，别把用户误踢出去。
      setCachedAuthSession(storedSession);
      return storedSession;
    }
    await clearStoredAuthSession();
    return null;
  }
}

/** 同一时刻只让一个校验请求在飞：首屏时侧边栏和页面守卫会同时问，合并成一次。 */
function validateOnce() {
  if (!inflightValidation) {
    inflightValidation = validate().finally(() => {
      inflightValidation = null;
    });
  }
  return inflightValidation;
}

export function getValidatedAuthSession(): Promise<StoredAuthSession | null> {
  const cached = getCachedAuthSession();

  if (!cached) {
    // 首次进站 / 刷新：没有可复用的结果，只能等网络。
    return validateOnce();
  }

  if (!cached.session) {
    // 明确未登录，直接给结论，不用再问一次。
    return Promise.resolve(null);
  }

  if (Date.now() - cached.checkedAt >= SESSION_TRUST_MS) {
    // 缓存过期：先把旧结果交出去让页面立刻渲染，复检在后台跑。
    void validateOnce();
  }

  return Promise.resolve(cached.session);
}

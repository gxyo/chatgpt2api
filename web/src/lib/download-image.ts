import { downloadSingleImage } from "@/lib/api";

/** 从图片 URL / data URL 推断下载文件名，保住 .gif 这类扩展名。 */
export function imageDownloadFilename(src: string, fallbackBase = "image"): string {
  if (src.startsWith("data:")) {
    const semi = src.indexOf(";");
    const mime = (semi === -1 ? src.slice(5) : src.slice(5, semi)).toLowerCase();
    const ext = mime.split("/")[1] || "png";
    return `${fallbackBase}.${ext === "jpeg" ? "jpg" : ext}`;
  }
  try {
    const name = decodeURIComponent(new URL(src, window.location.href).pathname.split("/").pop() || "");
    if (/\.[a-z0-9]+$/i.test(name)) return name;
  } catch {
    // 非法 URL 交给下面的兜底文件名。
  }
  return `${fallbackBase}.png`;
}

/**
 * 从图片 URL 里取出本机图片存储的相对路径（`/images/<rel>` → `<rel>`）。
 *
 * 日志里的图片 URL 是按 config.base_url 拼出来的，可能和后台页面不同域；浏览器
 * 直接 fetch 会因为没有 CORS 报 "Failed to fetch"（<img> 显示图片不需要 CORS，
 * 所以预览正常）。拿到相对路径后改走同源的带鉴权下载接口即可。
 */
export function imageStoragePathFromUrl(src: string): string | null {
  if (!src.startsWith("http://") && !src.startsWith("https://") && !src.startsWith("/")) {
    return null;
  }
  try {
    const pathname = new URL(src, window.location.href).pathname;
    const marker = "/images/";
    const index = pathname.indexOf(marker);
    if (index === -1) return null;
    const rel = decodeURIComponent(pathname.slice(index + marker.length));
    if (!rel || rel.includes("..")) return null;
    return rel;
  } catch {
    return null;
  }
}

async function downloadByFetch(src: string, fallbackBase: string): Promise<void> {
  const response = await fetch(src, { cache: "no-store" });
  if (!response.ok) {
    throw new Error(`下载失败（HTTP ${response.status}）`);
  }
  const blob = await response.blob();
  const objectUrl = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = objectUrl;
  link.download = imageDownloadFilename(src, fallbackBase);
  document.body.appendChild(link);
  link.click();
  document.body.removeChild(link);
  URL.revokeObjectURL(objectUrl);
}

/**
 * 下载图片的原始字节（不经过剪贴板、也不经过 <img> 解码）。
 *
 * 日志里的 GIF 直接用浏览器「复制图片」拿到的是渲染后的图片，粘出来会变成
 * 静态图；这里把服务器上的文件当作二进制抓下来，.gif 的动画和扩展名都能保住。
 */
export async function downloadImageFile(src: string, fallbackBase = "image"): Promise<void> {
  const storagePath = imageStoragePathFromUrl(src);
  if (storagePath) {
    try {
      // 同源 + 带鉴权，不受图片域名 CORS 限制。
      await downloadSingleImage(storagePath);
      return;
    } catch (error) {
      // 图片不属于本地存储（或当前身份无权限）时，再退回直接抓 URL。
      try {
        await downloadByFetch(src, fallbackBase);
        return;
      } catch {
        throw error;
      }
    }
  }
  await downloadByFetch(src, fallbackBase);
}

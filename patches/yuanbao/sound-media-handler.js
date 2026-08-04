/**
 * TIMSoundElem message handler.
 *
 * Yuanbao voice messages carry their downloadable audio URL in slightly
 * different field names between client versions.  Keep the extraction here
 * tolerant and expose the sound as ordinary media so download-media can cache
 * it before the deterministic audio-quality ingress runs.
 */
import { sanitizeMediaFilename } from "../../utils/media.js";

const URL_KEYS = [
    "url",
    "URL",
    "download_url",
    "Download_Url",
    "downloadUrl",
    "DownloadUrl",
    "resource_url",
    "resourceUrl",
];
const NAME_KEYS = [
    "file_name",
    "filename",
    "fileName",
    "name",
    "sound_name",
    "soundName",
];
const FORMAT_KEYS = ["sound_format", "soundFormat", "format", "Format"];

function firstString(value, keys) {
    if (!value || typeof value !== "object") {
        return "";
    }
    for (const key of keys) {
        const candidate = value[key];
        if (typeof candidate === "string" && candidate.trim()) {
            return candidate.trim();
        }
    }
    return "";
}

function extensionForFormat(format) {
    const normalized = format.toLowerCase().replace(/^\./, "");
    if (!normalized) {
        return ".amr";
    }
    return `.${normalized === "mpeg" ? "mp3" : normalized}`;
}

function ensureAudioExtension(name, format) {
    return /\.[a-z0-9]{2,5}$/i.test(name) ? name : `${name}${extensionForFormat(format)}`;
}

export const soundHandler = {
    msgType: "TIMSoundElem",
    extract(_ctx, elem, resData) {
        const content = elem?.msg_content ?? elem?.msgContent ?? elem;
        const url = typeof content === "string"
            ? content.trim()
            : firstString(content, URL_KEYS);
        if (!url) {
            return "[voice]";
        }
        const suppliedName = typeof content === "object" ? firstString(content, NAME_KEYS) : "";
        const format = typeof content === "object" ? firstString(content, FORMAT_KEYS) : "";
        const mediaName = sanitizeMediaFilename(
            ensureAudioExtension(suppliedName || "voice", format),
            `voice${extensionForFormat(format)}`,
        );
        resData.medias.push({ mediaType: "file", url, mediaName });
        return `[voice:${mediaName}]`;
    },
};

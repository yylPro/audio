/**
 * Actions adapter layer — unified entry.
 *
 * Proactive sends invoked by the OpenClaw CLI must be relayed to the running
 * Gateway.  Starting a second Yuanbao client for the same bot instance makes
 * Yuanbao close the inbound WebSocket with `instanceid conflict`.
 */
import { handleAction } from "./handler.js";

export { handleAction };

const SUPPORTED_ACTIONS = ["sticker-search", "sticker", "react", "send"];

function describeMessageTool() {
    return { actions: SUPPORTED_ACTIONS };
}

function listActions() {
    return SUPPORTED_ACTIONS;
}

export const yuanbaoMessageActions = {
    describeMessageTool,
    handleAction,
    listActions,
    supportsAction: ({ action }) => SUPPORTED_ACTIONS.includes(action),
    // CLI sends must use the existing Gateway connection, not create a second
    // Yuanbao WebSocket client with the same instance ID.
    resolveExecutionMode: ({ action }) => action === "send" ? "gateway" : "local",
    requiresTrustedRequesterSender: () => false,
};

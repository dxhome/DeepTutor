"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import { apiFetch, apiUrl } from "@/lib/api";
import { stripAudioMimeParameters } from "@/lib/voice-mime";

export type RecorderState = "idle" | "requesting" | "recording" | "transcribing";

/**
 * Microphone capture → backend transcription. Records via MediaRecorder, posts
 * the clip to ``/api/voice/stt`` (which uses the admin-configured STT
 * provider), and hands the transcript back through ``onTranscript``.
 */
export function useVoiceRecorder(onTranscript: (text: string) => void) {
  const { t } = useTranslation();
  const [state, setState] = useState<RecorderState>("idle");
  const [error, setError] = useState<string | null>(null);
  const [canRecord, setCanRecord] = useState(false);
  const [insecureContext, setInsecureContext] = useState(false);
  const recorderRef = useRef<MediaRecorder | null>(null);
  const chunksRef = useRef<Blob[]>([]);
  const streamRef = useRef<MediaStream | null>(null);
  const mountedRef = useRef(true);
  const onTranscriptRef = useRef(onTranscript);
  onTranscriptRef.current = onTranscript;

  const releaseStream = useCallback(() => {
    streamRef.current?.getTracks().forEach((track) => track.stop());
    streamRef.current = null;
  }, []);

  const transcribeBlob = useCallback(async (blob: Blob, filename: string) => {
    if (!blob.size) {
      setError(t("No audio was captured. Try recording again."));
      setState("idle");
      return;
    }
    setError(null);
    setState("transcribing");
    try {
      const form = new FormData();
      form.append("file", blob, filename);
      const resp = await apiFetch(apiUrl("/api/voice/stt"), {
        method: "POST",
        body: form,
      });
      if (!resp.ok) {
        const detail = (await resp.json().catch(() => null)) as { detail?: string } | null;
        throw new Error(detail?.detail || `Transcription failed (HTTP ${resp.status}).`);
      }
      const data = (await resp.json()) as { text?: string };
      const text = (data.text || "").trim();
      if (text && mountedRef.current) onTranscriptRef.current(text);
    } catch (err) {
      if (mountedRef.current) {
        setError(err instanceof Error ? err.message : t("Transcription failed."));
      }
    } finally {
      if (mountedRef.current) setState("idle");
    }
  }, [t]);

  const uploadAudio = useCallback((file: File) => {
    if (state !== "idle") return;
    void transcribeBlob(file, file.name || "recording.webm");
  }, [state, transcribeBlob]);

  const start = useCallback(async () => {
    if (state !== "idle") return;
    setError(null);
    if (!window.isSecureContext) {
      setError(t("Live microphone recording requires HTTPS on another device. Use the audio file option here, or open DeepTutor through a trusted HTTPS address."));
      return;
    }
    if (!navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === "undefined") {
      setError(t("This browser cannot use live microphone recording here. Use the audio file option instead."));
      return;
    }
    setState("requesting");
    let stream: MediaStream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch (err) {
      if (!mountedRef.current) return;
      const name = err instanceof DOMException ? err.name : "";
      setError(name === "NotAllowedError" || name === "PermissionDeniedError"
        ? t("Microphone access was denied. Allow the microphone for this site in your browser settings, then try again.")
        : name === "NotFoundError" || name === "DevicesNotFoundError"
          ? t("No microphone was found. Connect one and try again.")
          : t("Could not access the microphone. Check your browser and system microphone permissions, then try again."));
      setState("idle");
      return;
    }
    if (!mountedRef.current) {
      stream.getTracks().forEach((track) => track.stop());
      return;
    }
    streamRef.current = stream;
    let recorder: MediaRecorder;
    try {
      recorder = new MediaRecorder(stream);
    } catch {
      releaseStream();
      setError(t("This browser cannot record audio from the microphone."));
      setState("idle");
      return;
    }
    chunksRef.current = [];
    recorder.ondataavailable = (event) => {
      if (event.data && event.data.size > 0) chunksRef.current.push(event.data);
    };
    recorder.onstop = async () => {
      const mimeType = stripAudioMimeParameters(recorder.mimeType);
      releaseStream();
      const blob = new Blob(chunksRef.current, { type: mimeType });
      chunksRef.current = [];
      const ext = mimeType.includes("ogg")
        ? "ogg"
        : mimeType.includes("mp4")
          ? "mp4"
          : "webm";
      await transcribeBlob(blob, `recording.${ext}`);
    };
    try {
      recorder.start();
    } catch {
      releaseStream();
      setError(t("Could not start recording. Check whether another app is using the microphone."));
      setState("idle");
      return;
    }
    recorderRef.current = recorder;
    setState("recording");
  }, [releaseStream, state, t, transcribeBlob]);

  const stop = useCallback(() => {
    const recorder = recorderRef.current;
    if (recorder && recorder.state !== "inactive") {
      setState("transcribing");
      recorder.stop(); // fires onstop → transcribe
    }
  }, []);

  const toggle = useCallback(() => {
    if (state === "recording") stop();
    else if (state === "idle") void start();
  }, [start, state, stop]);

  // Stop the mic if the component unmounts mid-recording.
  useEffect(() => {
    mountedRef.current = true;
    setInsecureContext(!window.isSecureContext);
    setCanRecord(Boolean(window.isSecureContext && typeof navigator.mediaDevices?.getUserMedia === "function" && typeof MediaRecorder !== "undefined"));
    return () => {
      mountedRef.current = false;
      const recorder = recorderRef.current;
      if (recorder && recorder.state !== "inactive") {
        recorder.onstop = null;
        recorder.stop();
      }
      streamRef.current?.getTracks().forEach((track) => track.stop());
    };
  }, []);

  return { state, error, canRecord, insecureContext, toggle, start, stop, uploadAudio };
}

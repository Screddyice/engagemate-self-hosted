export function brandVoiceFromAnalysis(analysis: unknown): string | null {
  if (!analysis || typeof analysis !== "object" || Array.isArray(analysis)) {
    return null;
  }

  const voice = (analysis as Record<string, unknown>).voice_summary;
  if (typeof voice !== "string" || voice.trim() === "") return null;
  return voice.trim();
}

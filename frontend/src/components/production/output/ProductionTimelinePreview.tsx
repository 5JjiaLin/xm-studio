import { useCallback, useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { API } from "@/api";
import { EditTimelinePlayer } from "@/components/canvas/edit/EditTimelinePlayer";
import { buildPlaybackPlan, type PlaybackSegment } from "@/components/canvas/edit/playback-schedule";
import { bgmPlacements, narrationPlacements, placeSubtitles, type AudioPlacement } from "@/components/canvas/edit/preview-tracks";
import { useTimelinePlayback } from "@/components/canvas/edit/useTimelinePlayback";
import type { EditTimelineReadout, EditTimelinePreviewMedia } from "@/types/edit-timeline";
import type { ProductionEdit } from "@/types/production";
import type { ReferenceVideoUnit } from "@/types/reference-video";
import { previewAspect } from "@/utils/preview-aspect";
import { errMsg } from "@/utils/async";

/** One compact player reuses the canonical global picture, speech and music clock. */
export function ProductionTimelinePreview({ project, data, units, dirty }: {
  project: string; data: ProductionEdit; units: ReferenceVideoUnit[]; dirty: boolean;
}) {
  const { t } = useTranslation("dashboard");
  const [loaded, setLoaded] = useState<{ readout: EditTimelineReadout; media: EditTimelinePreviewMedia } | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    void Promise.all([
      API.getEditTimeline(project, data.timeline_id, { signal: controller.signal }),
      API.getEditTimelinePreviewMedia(project, data.timeline_id, { signal: controller.signal, narration: "without_narration" }),
    ]).then(([readout, media]) => {
      if (readout.revision !== data.revision || media.revision !== data.revision) throw new Error(t("music_preview_expired"));
      if (!controller.signal.aborted) setLoaded({ readout, media });
    }).catch((cause) => { if (!controller.signal.aborted) setError(errMsg(cause)); });
    return () => controller.abort();
  }, [project, data.timeline_id, data.revision, t]);
  return <section className="flex flex-col gap-2" aria-label={t("music_preview")}>
    <h3 className="font-medium">{t("music_preview")}</h3>
    {dirty ? <p role="status">{t("music_save_first")}</p> : loaded ? <Player project={project} data={data} units={units} {...loaded} /> : <p role="status">{error || t("common:loading")}</p>}
  </section>;
}
function Player({ project, data, units, readout, media }: {
  project: string; data: ProductionEdit; units: ReferenceVideoUnit[]; readout: EditTimelineReadout; media: EditTimelinePreviewMedia;
}) {
  const { t } = useTranslation("dashboard");
  const [showSubtitles, setShowSubtitles] = useState(true);
  const plan = useMemo(() => buildPlaybackPlan(readout, media), [readout, media]);
  const sourceUrl = useCallback((segment: PlaybackSegment) => {
    const file = units.find((unit) => unit.unit_id === segment.unitId)?.generated_assets?.video_clip;
    return file && segment.hasVideo ? API.getFileUrl(project, file, segment.videoVersion) : null;
  }, [project, units]);
  const audio = useMemo(() => [...narrationPlacements(readout, media), ...bgmPlacements(readout, media)], [readout, media]);
  const audioUrl = useCallback((placement: AudioPlacement) => {
    if (placement.kind === "bgm") {
      const track = media.bgm.find((item) => item.bgm_id === placement.sourceId);
      return track ? API.getFileUrl(project, track.path, null) : null;
    }
    const source = media.units.find((item) => item.unit_id === placement.sourceId)?.narration_audio;
    return source ? API.getFileUrl(project, source.path, source.version) : null;
  }, [project, media]);
  const playback = useTimelinePlayback(plan, sourceUrl, audio, audioUrl);
  const subtitles = useMemo(() => {
    const manual = new Set(data.clips.filter((clip) => clip.subtitles !== null).map((clip) => clip.id));
    const auto = placeSubtitles({ ...readout, clips: readout.clips.filter((clip) => !manual.has(clip.id)) }, { ...plan, segments: plan.segments.filter((segment) => !manual.has(segment.clipId)) }, media);
    const placed = plan.segments.flatMap((segment) => (data.clips.find((clip) => clip.id === segment.clipId)?.subtitles ?? []).flatMap((cue) => {
      if (cue.basis_version !== segment.videoVersion) return [];
      const start = Math.max(segment.sourceIn, cue.start_us / 1e6);
      const end = Math.min(segment.sourceOut, cue.end_us / 1e6);
      return end > start ? [{ start: segment.start + start - segment.sourceIn, end: segment.start + end - segment.sourceIn, text: cue.text }] : [];
    }));
    return [...auto, ...placed].sort((a, b) => a.start - b.start);
  }, [data, readout, plan, media]);
  const positions = useMemo(() => new Map(units.map((unit, index) => [unit.unit_id, index + 1])), [units]);
  return <div className="flex flex-col gap-2">
    <div className="h-96"><EditTimelinePlayer plan={plan} playback={playback} aspect={previewAspect(data)} current={readout.clips.find((clip) => clip.id === plan.segments[playback.index]?.clipId)} positions={positions} trimIgnored={false} subtitles={subtitles} showSubtitles={showSubtitles} onToggleSubtitles={() => setShowSubtitles((value) => !value)} /></div>
    <input type="range" aria-label={t("music_seek")} min={0} max={plan.duration} step={0.01} value={playback.t} onChange={(event) => playback.seek(Number(event.target.value))} />
    <p className="text-xs text-muted-foreground">{t("music_preview_hint")}</p>
  </div>;
}

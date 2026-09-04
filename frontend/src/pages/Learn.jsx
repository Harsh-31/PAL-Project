import { useEffect, useRef, useState } from 'react';
import YouTube from 'react-youtube';
import { api } from '../api/client';
import { useAuth } from '../contexts/AuthContext';
import QuizOverlay from '../components/QuizOverlay';
import NotesTab from '../components/NotesTab';
import ChatTab from '../components/ChatTab';
import CodeTab from '../components/CodeTab';

const fmt = (s) => {
  const m = Math.floor(s / 60), sec = Math.floor(s % 60);
  return `${m}:${sec.toString().padStart(2, '0')}`;
};

export default function Learn() {
  const { user } = useAuth();
  const [lectures, setLectures] = useState([]);
  const [activeLecture, setActiveLecture] = useState(null);
  const [activeChunk, setActiveChunk] = useState(null);
  const [currentTime, setCurrentTime] = useState(0);
  const [tab, setTab] = useState('notes');
  const [quizChunk, setQuizChunk] = useState(null);
  const [palAction, setPalAction] = useState(null);  // { text, tone } — shown as toast for a few seconds
  const [triggeredChunks, setTriggeredChunks] = useState(() => new Set());
  const [completedLectures, setCompletedLectures] = useState(() => new Set());
  const playerRef = useRef(null);
  const timerRef = useRef(null);

  const trackIds = user?.track_ids || [];
  const targetConcepts = user?.target_concept_ids || [];
  const hasTracks = trackIds.length > 0 || targetConcepts.length > 0;

  const [masteryMap, setMasteryMap] = useState({});  // concept_id -> {score, attempts}

  const sortByMastery = (lectures, mMap) => {
    const conceptMastery = (lec) => {
      // Each lecture teaches (at least) one concept via its chunks
      const cid = lec.chunks[0]?.concept_id;
      return cid ? (mMap[cid]?.score ?? -1) : -1;  // -1 means not-yet-attempted
    };
    // Struggling first (score in [0, 0.55)), then not-attempted (-1),
    // then progressing (0.55–0.85), then mastered (>= 0.85) last.
    const bucket = (score) => {
      if (score < 0)     return 1;   // not attempted
      if (score < 0.55)  return 0;   // struggling — bubble to top
      if (score < 0.85)  return 2;   // progressing
      return 3;                       // mastered — sink to bottom
    };
    return [...lectures].sort((a, b) => {
      const ba = bucket(conceptMastery(a));
      const bb = bucket(conceptMastery(b));
      if (ba !== bb) return ba - bb;
      return a.title.localeCompare(b.title);
    });
  };

  const refreshPlaylist = async (preserveActive = true) => {
    if (!hasTracks) return;
    let data;
    try {
      data = await api('/api/playlist');
    } catch (e) {
      // Keep whatever playlist is already on screen — don't blank the page
      // or crash the learning flow over a transient fetch failure.
      console.error('Playlist refresh failed:', e.message);
      showPalAction("We couldn't refresh your playlist right now. Your current playlist is still available.", 'warn');
      return;
    }
    const mMap = Object.fromEntries((data.mastery || []).map(m => [m.id, m]));
    setMasteryMap(mMap);
    // Merge in supplementary lectures from other courses (PRD: cross-course curation)
    const supp = (data.supplementary || []).map(l => ({ ...l, supplementary: true }));
    const merged = [...data.lectures, ...supp];
    const sorted = sortByMastery(merged, mMap);
    setLectures(sorted);
    if (!preserveActive || !activeLecture) {
      if (sorted[0]) {
        setActiveLecture(sorted[0]);
        setActiveChunk(sorted[0].chunks[0]);
      }
    }
  };

  useEffect(() => { refreshPlaylist(false); }, [hasTracks]);

  // Poll player time — used both to render "current chunk" and to trigger quizzes
  useEffect(() => {
    if (!playerRef.current) return;
    if (timerRef.current) clearInterval(timerRef.current);
    timerRef.current = setInterval(async () => {
      const p = playerRef.current;
      if (!p || typeof p.getCurrentTime !== 'function') return;
      const t = await p.getCurrentTime();
      setCurrentTime(t);

      if (!activeLecture) return;
      // Find the chunk we're inside
      const cur = activeLecture.chunks.find(c => t >= c.start && t < c.end);
      if (cur && cur.id !== activeChunk?.id) setActiveChunk(cur);

      // Fire quiz once when a chunk ends
      for (const ch of activeLecture.chunks) {
        // Small tolerance window: trigger between end - 0.5 and end + 1.5
        if (t >= ch.end - 0.5 && t <= ch.end + 1.5 && !triggeredChunks.has(ch.id)) {
          setTriggeredChunks(prev => new Set(prev).add(ch.id));
          await p.pauseVideo();
          setQuizChunk(ch);
          break;
        }
      }
    }, 1000);
    return () => clearInterval(timerRef.current);
  }, [activeLecture, activeChunk, triggeredChunks]);

  const pickLecture = (lec) => {
    setActiveLecture(lec);
    setActiveChunk(lec.chunks[0]);
    setTriggeredChunks(new Set());
  };

  const jumpToChunk = (ch) => {
    setActiveChunk(ch);
    if (playerRef.current?.seekTo) playerRef.current.seekTo(ch.start, true);
  };

  const showPalAction = (text, tone = 'info') => {
    setPalAction({ text, tone });
    setTimeout(() => setPalAction(null), 6000);
  };

  const completeLecture = async (lec) => {
    if (!lec || completedLectures.has(lec.lecture_id)) return;
    setCompletedLectures(prev => new Set(prev).add(lec.lecture_id));
    // Snapshot the current supplementary count so we can announce any additions
    const preSupp = lectures.filter(l => l.supplementary).length;
    await refreshPlaylist(true);
    // Re-read fresh supplementary count via a second call — cheap and simple
    let addedSupp = 0;
    try {
      const data = await api('/api/playlist');
      const nowSupp = (data.supplementary || []).length;
      addedSupp = Math.max(0, nowSupp - preSupp);
    } catch {}
    const msg = addedSupp > 0
      ? `Lecture complete — PAL added ${addedSupp} supplementary lecture${addedSupp > 1 ? 's' : ''} from other courses to help with weak topics.`
      : `Lecture complete — PAL rebuilt your playlist based on your performance across "${lec.title}".`;
    showPalAction(msg, addedSupp > 0 ? 'warn' : 'info');
  };

  // Merge Recommendation-Engine results from a quiz submission into the
  // sidebar list, reusing the exact same shape/rendering `refreshPlaylist`
  // already uses for playlist-level supplementary lectures (lines below,
  // `lec.supplementary` branch) — no separate UI needed.
  const mergeRecommendations = (recs) => {
    if (!recs || !recs.length) return;
    setLectures(prev => {
      const existingIds = new Set(prev.map(l => l.lecture_id));
      const fresh = recs.filter(r => !existingIds.has(r.lecture_id))
        .map(r => ({ ...r, supplementary: true }));
      return fresh.length ? [...prev, ...fresh] : prev;
    });
  };

  // Remove sidebar cards for remediation the backend just retired (learner
  // mastered the concept it was recommended for) — see
  // AdaptiveLearningOrchestrator's Mastered handling.
  const retireLectures = (retiredIds) => {
    if (!retiredIds || !retiredIds.length) return;
    setLectures(prev => prev.filter(l => !retiredIds.includes(l.lecture_id)));
  };

  const onQuizClose = async (result) => {
    const ch = quizChunk;
    setQuizChunk(null);
    mergeRecommendations(result?.recommendations);
    retireLectures(result?.retired_lecture_ids);

    // Lecture-complete check: quizzes fire at every chunk boundary except the final
    // chunk (which has no boundary after it). So if the just-answered chunk was the
    // second-to-last chunk in the lecture, all quizzes have been offered → lecture done.
    if (ch && activeLecture && result) {
      const chunks = activeLecture.chunks;
      const idx = chunks.findIndex(c => c.id === ch.id);
      const isLastQuizChunk = idx === chunks.length - 2;
      if (isLastQuizChunk) {
        // Defer the completion action until after the intervention plays out below
        setTimeout(() => completeLecture(activeLecture), 800);
      }
    }

    // Act on the Process KG intervention. This is what makes the loop visible.
    const action = result?.intervention?.action;
    const wasCorrect = result?.correct;
    const player = playerRef.current;
    if (!ch || !player) {
      if (player?.playVideo) player.playVideo();
      return;
    }

    // If the answer was correct, never rewind — mastery may still be in the Struggling
    // band while it recovers, but rewinding after a correct answer is pedagogically wrong.
    // The only "positive" action we honour on correct is skip-forward when mastered.
    //
    // All toast text below is deliberately plain, pedagogical language — it
    // must never expose internal Process KG state names (Struggling/
    // OnTrack/Mastered), rule/action identifiers, thresholds, or
    // recommendation-engine terminology to the learner. The `action` string
    // itself is only ever used as an internal switch key.
    if (wasCorrect) {
      if (action === 'skip_next_similar_chunk') {
        const chunks = activeLecture?.chunks || [];
        const idx = chunks.findIndex(c => c.id === ch.id);
        const skipTarget = chunks.slice(idx + 1).find(c => c.concept_id === ch.concept_id);
        if (skipTarget) {
          setActiveChunk(skipTarget);
          player.seekTo(skipTarget.end, true);
          setTriggeredChunks(prev => new Set(prev).add(skipTarget.id));
          player.playVideo();
          showPalAction("You've mastered this concept. Let's move ahead.", 'ok');
          return;
        }
      }
      player.playVideo();
      if (action === 'offer_challenge_content') {
        // Question difficulty is decided independently by the RL controller
        // (see result.next_difficulty) — this message never mentions
        // enrichment/challenge content, since none is added here.
        showPalAction("Good progress. Let's continue.", 'ok');
      } else {
        showPalAction("You're on the right track. Let's continue.", 'ok');
      }
      return;
    }

    // Wrong answer path — apply the Process KG remediation action. The
    // normal explanation and (for wrong answers) the analogy were already
    // shown in QuizOverlay's result panel, independent of this switch —
    // these toasts only describe the pedagogical action being taken next.
    switch (action) {
      case 'simplify_with_hobby_analogy':
        player.seekTo(ch.start, true);
        player.pauseVideo();
        showPalAction("Let's look at this another way.", 'warn');
        break;
      case 'insert_prerequisite_video':
        player.seekTo(ch.start, true);
        player.playVideo();
        showPalAction(
          // Only mention added content when something was actually added —
          // never imply a video was added if it wasn't.
          result?.recommendations?.length
            ? `Let's review this concept — added ${result.recommendations.length} helpful lecture${result.recommendations.length > 1 ? 's' : ''} to your sidebar.`
            : "Let's review this concept before moving on.",
          'warn'
        );
        break;
      case 'offer_challenge_content':
        player.playVideo();
        showPalAction("Good progress. Let's continue.", 'ok');
        break;
      case 'skip_next_similar_chunk': {
        // Find the next chunk in the same lecture that teaches the same concept
        const chunks = activeLecture?.chunks || [];
        const idx = chunks.findIndex(c => c.id === ch.id);
        const skipTarget = chunks.slice(idx + 1).find(c => c.concept_id === ch.concept_id);
        if (skipTarget) {
          setActiveChunk(skipTarget);
          player.seekTo(skipTarget.end, true);  // jump past the redundant chunk
          setTriggeredChunks(prev => new Set(prev).add(skipTarget.id));  // don't quiz it either
          player.playVideo();
        } else {
          player.playVideo();
        }
        // Same message either way — from the learner's perspective, both
        // cases simply mean "you've got this, moving on." Nothing was
        // deleted; at most, playback moved past an already-mastered section.
        showPalAction("You've mastered this concept. Let's move ahead.", 'ok');
        break;
      }
      case 'continue_normal':
      default:
        player.playVideo();
        break;
    }
  };

  if (!lectures.length) return <div className="center-full">Loading playlist…</div>;

  const opts = {
    playerVars: { modestbranding: 1, rel: 0 },
  };

  return (
    <div className="learn-shell">
      <div className="playlist-panel">
        <h3>Your Playlist</h3>
        {lectures.map(lec => {
          const cid = lec.chunks[0]?.concept_id;
          const score = cid ? masteryMap[cid]?.score : undefined;
          let badge = null, dim = false;
          if (lec.supplementary) {
            badge = lec.challenge
              ? <span className="lec-badge suppl">🚀 Enrichment</span>
              : <span className="lec-badge suppl">📚 Bridge lecture</span>;
          } else if (score !== undefined) {
            if (score < 0.55) badge = <span className="lec-badge focus">🎯 Focus</span>;
            else if (score >= 0.85) { badge = <span className="lec-badge mastered">✓ Mastered</span>; dim = true; }
            else badge = <span className="lec-badge progress">{Math.round(score * 100)}%</span>;
          }
          const durLabel = lec.duration_sec ? fmt(lec.duration_sec) : null;
          return (
          <div
            key={lec.lecture_id}
            className={`playlist-item ${lec.lecture_id === activeLecture?.lecture_id ? 'active' : ''} ${dim ? 'dim' : ''} ${lec.supplementary ? 'suppl' : ''}`}
          >
            <div className="lec-title" onClick={() => pickLecture(lec)}>
              <span>
                {lec.supplementary && <span className="play-icon">▶</span>}
                {lec.title}
                {durLabel && <span className="lec-duration"> · {durLabel}</span>}
              </span>
              {badge}
            </div>
            {lec.supplementary && (
              <div className="suppl-meta" onClick={() => pickLecture(lec)}>
                {lec.challenge
                  ? <>PAL found this to go deeper after <b>{lec.for_concept_name}</b></>
                  : <>PAL recommended this to strengthen <b>{lec.for_concept_name}</b></>}
                {typeof lec.similarity === 'number' && (
                  <span className="suppl-sim"> · {Math.round(lec.similarity * 100)}% match</span>
                )}
                <div className="suppl-source">Source: {lec.source_course_title}</div>
              </div>
            )}
            {lec.lecture_id === activeLecture?.lecture_id &&
              lec.chunks.map(ch => (
                <div
                  key={ch.id}
                  className={`chunk ${ch.id === activeChunk?.id ? 'current' : ''}`}
                  onClick={() => jumpToChunk(ch)}
                >
                  ▸ {ch.concept_name} · {fmt(ch.start)}–{fmt(ch.end)}
                </div>
              ))}
          </div>
          );
        })}
      </div>

      <div className="player-panel">
        {activeLecture && (
          <>
            <div className="video-wrap">
              <YouTube
                videoId={activeLecture.youtube_id}
                opts={opts}
                onReady={e => (playerRef.current = e.target)}
                onEnd={() => completeLecture(activeLecture)}
                iframeClassName=""
                style={{ width: '100%', height: '100%' }}
              />
            </div>
            <h2>{activeLecture.title}</h2>
          </>
        )}
      </div>

      <div className="sidebar-panel">
        <div className="sidebar-tabs">
          <button className={tab === 'notes' ? 'active' : ''} onClick={() => setTab('notes')}>Notes</button>
          <button className={tab === 'chat' ? 'active' : ''} onClick={() => setTab('chat')}>Chat</button>
          <button className={tab === 'code' ? 'active' : ''} onClick={() => setTab('code')}>Code</button>
        </div>
        <div className="sidebar-body">
          {tab === 'notes' && <NotesTab videoId={activeLecture?.lecture_id} currentTime={currentTime} />}
          {tab === 'chat' && <ChatTab lectureId={activeLecture?.lecture_id} chunkId={activeChunk?.id} />}
          {tab === 'code' && <CodeTab />}
        </div>
      </div>

      {palAction && (
        <div className={`pal-toast pal-toast-${palAction.tone}`}>
          {palAction.text}
        </div>
      )}

      {quizChunk && (
        <QuizOverlay
          lectureId={activeLecture.lecture_id}
          chunk={quizChunk}
          onClose={onQuizClose}
        />
      )}
    </div>
  );
}
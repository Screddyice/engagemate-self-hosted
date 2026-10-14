import Image from "next/image";
import type { ReactNode } from "react";

/**
 * Where a Story reply actually lands, drawn rather than screenshotted.
 *
 * These are deliberately our own shapes and type, not a pixel recreation of
 * Instagram's interface: the site must not ship anything a visitor could mistake
 * for a real capture of another company's product, and we have no rights to
 * their UI or to anyone's photo. The caption says so. What is true here is the
 * mechanic — a reply to a Story opens a DM thread attached to that Story — and
 * the reply text is the same one the worker generated in the demo run.
 */

function PhoneFrame({ children }: { children: ReactNode }) {
  return (
    <div
      className="relative mx-auto w-full max-w-[300px] rounded-[28px] border p-2"
      style={{
        borderColor: "var(--w-border)",
        background: "var(--w-surface)",
        boxShadow: "var(--w-shot-shadow), var(--w-glass)",
      }}
    >
      <div
        className="overflow-hidden rounded-[22px]"
        style={{ background: "#12121a" }}
      >
        {children}
      </div>
    </div>
  );
}

/**
 * The Story the worker replies to. A simple illustrated frame, so the reply
 * can name something you can actually see in it.
 */
function PoolStory() {
  return (
    <div className="relative h-[320px] w-full overflow-hidden">
      <Image
        src="/story/pool.jpg"
        alt="An illustrated curved swimming pool with a mosaic tile edge beside a stone house"
        width={620}
        height={1102}
        className="absolute inset-0 h-full w-full object-cover"
        sizes="300px"
      />
      {/* the gradient Stories put behind the header and reply bar */}
      <div
        className="absolute inset-x-0 top-0 h-24"
        style={{ background: "linear-gradient(180deg,rgba(0,0,0,0.45),transparent)" }}
      />
      <div
        className="absolute inset-x-0 bottom-0 h-32"
        style={{ background: "linear-gradient(0deg,rgba(0,0,0,0.5),transparent)" }}
      />
      <div className="absolute left-1/2 top-[16%] -translate-x-1/2">
        <span
          className="rounded-md px-2.5 py-1 text-[10px] font-semibold tracking-wide text-[#2a2418]"
          style={{ background: "rgba(255,255,255,0.94)" }}
        >
          finally warm enough
        </span>
      </div>
    </div>
  );
}

/**
 * The same picture, posted plainly with no question in it. That is the point of
 * this panel: identical picture, and the worker still picks a different action,
 * because the deciding factor is what the Story invites rather than what it
 * shows.
 */
function QuietStory() {
  return (
    <div className="relative h-[320px] w-full overflow-hidden">
      <Image
        src="/story/pool.jpg"
        alt="An illustrated curved swimming pool beside a stone house"
        width={620}
        height={1102}
        className="absolute inset-0 h-full w-full object-cover"
        sizes="300px"
      />
      <div
        className="absolute inset-x-0 top-0 h-24"
        style={{ background: "linear-gradient(180deg,rgba(0,0,0,0.45),transparent)" }}
      />
      <div
        className="absolute inset-x-0 bottom-0 h-32"
        style={{ background: "linear-gradient(0deg,rgba(0,0,0,0.5),transparent)" }}
      />
    </div>
  );
}

function StoryHeader({ handle, when }: { handle: string; when: string }) {
  return (
    <div className="absolute left-0 right-0 top-0 z-[2] px-3 pt-3">
      <div className="h-[2px] w-full rounded-full bg-white/25">
        <div className="h-[2px] w-[62%] rounded-full bg-white" />
      </div>
      <div className="mt-2.5 flex items-center gap-2">
        <span
          className="h-6 w-6 shrink-0 rounded-full"
          style={{
            background: "linear-gradient(135deg,#f0a05a,#d0526e)",
            boxShadow: "0 0 0 1.5px rgba(255,255,255,0.9)",
          }}
        />
        <span className="text-[12px] font-semibold text-white">{handle}</span>
        <span className="text-[11px] text-white/60">{when}</span>
      </div>
    </div>
  );
}

/**
 * Draws a ring around whatever it wraps, rather than at guessed coordinates,
 * so the annotation stays on target at any size. preserveAspectRatio="none"
 * lets the ellipse stretch to the wrapped box.
 */
function CircleAround({
  label,
  labelSide = "above",
  labelAlign = "center",
  children,
}: {
  label: string;
  labelSide?: "above" | "below";
  labelAlign?: "center" | "right";
  children: ReactNode;
}) {
  return (
    <span className="relative block">
      {children}
      <svg
        viewBox="0 0 100 100"
        preserveAspectRatio="none"
        aria-hidden="true"
        className="pointer-events-none absolute -inset-x-[7px] -inset-y-[9px] z-[4] h-[calc(100%+18px)] w-[calc(100%+14px)]"
      >
        <ellipse
          cx="50"
          cy="50"
          rx="49"
          ry="46"
          fill="none"
          stroke="var(--w-amber)"
          strokeWidth="1.6"
          vectorEffect="non-scaling-stroke"
          opacity="0.95"
        />
      </svg>
      <span
        className={`pointer-events-none absolute z-[5] whitespace-nowrap rounded-full px-2.5 py-1 font-mono text-[10px] font-semibold ${
          labelSide === "above" ? "bottom-full mb-3.5" : "top-full mt-3.5"
        } ${labelAlign === "right" ? "right-0" : "left-1/2 -translate-x-1/2"}`}
        style={{ background: "var(--w-amber)", color: "#1a1205" }}
      >
        {label}
      </span>
    </span>
  );
}


function Heart({ filled = true }: { filled?: boolean }) {
  return (
    <svg viewBox="0 0 24 24" className="h-5 w-5" aria-hidden="true">
      <path
        d="M12 20.7 3.9 12.9a5.1 5.1 0 0 1 7.2-7.2l.9.9.9-.9a5.1 5.1 0 0 1 7.2 7.2Z"
        fill={filled ? "#ff3b5c" : "none"}
        stroke={filled ? "#ff3b5c" : "rgba(255,255,255,0.85)"}
        strokeWidth="1.6"
        strokeLinejoin="round"
      />
    </svg>
  );
}

export function StoryReplyIllustration() {
  return (
    <div className="grid gap-16 sm:gap-10 lg:grid-cols-3">
      {/* 1. A like, when that is the whole move */}
      <figure>
        <PhoneFrame>
          <div className="relative">
            <QuietStory />
            <StoryHeader handle="jo.example" when="2h" />

            <div className="absolute bottom-0 left-0 right-0 flex items-center gap-2 p-3.5">
              <span
                className="flex-1 rounded-full px-3.5 py-2.5 text-[11px] text-white/55"
                style={{ border: "1px solid rgba(255,255,255,0.28)" }}
              >
                Send message
              </span>
              <CircleAround label="A like, sent" labelAlign="right">
                <span className="block p-1">
                  <Heart />
                </span>
              </CircleAround>
            </div>
          </div>
        </PhoneFrame>
        <figcaption className="mt-12 text-center text-[14px] leading-relaxed text-[var(--w-t3)]">
          <span className="font-semibold text-[var(--w-t1)]">
            1. Sometimes a like is enough.
          </span>{" "}
          The same Story, a different call. With nothing specific worth saying,
          or this account having heard from you recently, it acknowledges and
          moves on rather than forcing a reply.
        </figcaption>
      </figure>

      {/* 2. The target's Story, with the reply going out */}
      <figure>
        <PhoneFrame>
          <div className="relative">
            <PoolStory />
            <StoryHeader handle="jo.example" when="2h" />

            <div className="absolute bottom-0 left-0 right-0 p-3.5">
              <CircleAround label="EngageMate's reply">
                <span
                  className="block rounded-2xl px-3.5 py-2.5"
                  style={{
                    background: "rgba(255,255,255,0.13)",
                    border: "1px solid rgba(255,255,255,0.28)",
                    backdropFilter: "blur(6px)",
                  }}
                >
                  <span className="block text-[11.5px] leading-snug text-white">
                    That blue tile against the limestone is such a good call.
                    Did it come with the house?
                  </span>
                </span>
              </CircleAround>
            </div>
          </div>
        </PhoneFrame>
        <figcaption className="mt-12 text-center text-[14px] leading-relaxed text-[var(--w-t3)]">
          <span className="font-semibold text-[var(--w-t1)]">
            2. When there is something to say.
          </span>{" "}
          It read the actual photo, so the reply names the tile rather than
          saying &ldquo;looks great&rdquo;.
        </figcaption>
      </figure>

      {/* 3. Where a reply lands: their DMs */}
      <figure>
        <PhoneFrame>
          <div className="relative" style={{ background: "#12121a" }}>
            <div className="flex items-center gap-2 border-b border-white/10 px-3 py-3">
              {/* her avatar, same as the one on the Story she posted */}
              <span
                className="h-6 w-6 shrink-0 rounded-full"
                style={{ background: "linear-gradient(135deg,#f0a05a,#d0526e)" }}
              />
              <span className="text-[12px] font-semibold text-white">
                jo.example
              </span>
              <span className="ml-auto text-[10px] text-white/45">now</span>
            </div>

            <div className="space-y-2.5 px-3 py-4" style={{ height: 292 }}>
              <p className="text-center text-[10px] text-white/40">
                You replied to her story
              </p>

              {/* Seen from your phone: yours goes out, hers comes back. */}
              <div className="flex justify-end">
                <div className="max-w-[80%]">
                  {/* the same frame as the Story above, so the thread reads as
                      attached to that post rather than to some other picture */}
                  <Image
                    src="/story/pool.jpg"
                    alt="Thumbnail of the Story this reply is attached to"
                    width={620}
                    height={1102}
                    className="mb-1 ml-auto h-16 w-14 rounded-lg object-cover"
                    sizes="56px"
                  />
                  <CircleAround label="Lands as a DM" labelSide="below">
                    <span
                      className="block rounded-2xl px-3 py-2"
                      style={{ background: "#3b6fe0" }}
                    >
                      <span className="block text-[11.5px] leading-snug text-white">
                        That blue tile against the limestone is such a good
                        call. Did it come with the house?
                      </span>
                    </span>
                  </CircleAround>
                </div>
              </div>

              <div className="flex justify-start pt-7">
                <div
                  className="max-w-[74%] rounded-2xl px-3 py-2"
                  style={{ background: "rgba(255,255,255,0.10)" }}
                >
                  <p className="text-[11.5px] leading-snug text-white/90">
                    we picked it! took us months to decide
                  </p>
                </div>
              </div>
            </div>
          </div>
        </PhoneFrame>
        <figcaption className="mt-12 text-center text-[14px] leading-relaxed text-[var(--w-t3)]">
          <span className="font-semibold text-[var(--w-t1)]">
            3. Which lands in their DMs.
          </span>{" "}
          Attached to the Story they just posted, which is why it gets answered.
        </figcaption>
      </figure>
    </div>
  );
}

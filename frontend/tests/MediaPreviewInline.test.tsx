// MediaPreviewInline renders a compact card for a video/pdf file that can be
// expanded into an inline <video>/<iframe> player, and exposes the file name
// as a clickable link. Icon has its own concerns; mock it so this suite
// isolates the component's own render + interaction logic. setup.ts
// initializes i18n with empty resources + fallbackLng:false, so t(key)
// resolves to the key string itself.
vi.mock("../src/components/Icon", () => ({
  default: ({ name, size }: { name?: string; size?: number }) =>
    React.createElement("span", { "data-icon": name ?? "", "data-size": size ?? "" }),
}));

import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { MediaPreviewInline, getMediaType } from "../src/components/MediaPreviewInline";

afterEach(() => {
  vi.restoreAllMocks();
});

const ENC = (s: string) => encodeURIComponent(s);

describe("getMediaType", () => {
  it("detects each video extension", () => {
    for (const ext of ["mp4", "webm", "mov", "avi", "mkv", "m4v", "ogv", "3gp"]) {
      expect(getMediaType(`clip.${ext}`)).toBe("video");
    }
  });

  it("detects pdf", () => {
    expect(getMediaType("doc.pdf")).toBe("pdf");
  });

  it("is case-insensitive", () => {
    expect(getMediaType("DOC.PDF")).toBe("pdf");
    expect(getMediaType("CLIP.MP4")).toBe("video");
  });

  it("returns null for unknown extensions", () => {
    expect(getMediaType("archive.zip")).toBeNull();
  });

  it("returns null when there is no extension", () => {
    expect(getMediaType("README")).toBeNull();
  });

  it("returns null for a trailing dot", () => {
    expect(getMediaType("noext.")).toBeNull();
  });
});

describe("MediaPreviewInline", () => {
  function render_(over: { path?: string; mediaType?: "video" | "pdf"; onFileClick?: (p: string) => void } = {}) {
    const onFileClick = over.onFileClick ?? vi.fn();
    const path = over.path ?? "dir/clip.mp4";
    const mediaType = over.mediaType ?? "video";
    const result = render(
      <MediaPreviewInline path={path} mediaType={mediaType} onFileClick={onFileClick} />,
    );
    return { ...result, onFileClick, path };
  }

  it("shows the video icon and preview button when collapsed", () => {
    render_({ path: "a/b/clip.mp4", mediaType: "video" });
    expect(screen.getByText("mediaPreview.preview")).toBeTruthy();
    expect(screen.getByText("clip.mp4")).toBeTruthy(); // fileName splits on "/"
    expect(document.querySelector("[data-icon='film']")).toBeTruthy();
    expect(document.querySelector(".media-preview-content")).toBeNull();
  });

  it("shows the pdf icon when mediaType is pdf", () => {
    render_({ path: "doc.pdf", mediaType: "pdf" });
    expect(document.querySelector("[data-icon='memo']")).toBeTruthy();
  });

  it("uses the whole path as the name when there is no slash", () => {
    render_({ path: "plain.mp4" });
    expect(screen.getByText("plain.mp4")).toBeTruthy();
  });

  it("expands into a <video> player with the raw url and flips the button label", () => {
    const path = "dir/clip.webm";
    render_({ path, mediaType: "video" });
    fireEvent.click(screen.getByText("mediaPreview.preview"));
    const video = document.querySelector("video") as HTMLVideoElement;
    expect(video).toBeTruthy();
    expect(video.getAttribute("src")).toBe(`/api/file/raw?path=${ENC(path)}&node_id=primary`);
    expect(video.controls).toBe(true);
    expect(video.textContent).toContain("mediaPreview.videoNotSupported");
    expect(screen.getByText("mediaPreview.collapse")).toBeTruthy();
    expect(document.querySelector(".media-preview-inline.expanded")).toBeTruthy();
  });

  it("expands into an <iframe> for pdf with the raw url and title", () => {
    const path = "dir/doc.pdf";
    render_({ path, mediaType: "pdf" });
    fireEvent.click(screen.getByText("mediaPreview.preview"));
    const iframe = document.querySelector("iframe") as HTMLIFrameElement;
    expect(iframe).toBeTruthy();
    expect(iframe.getAttribute("src")).toBe(`/api/file/raw?path=${ENC(path)}&node_id=primary`);
    expect(iframe.getAttribute("title")).toBe("doc.pdf");
  });

  it("collapses again on a second toggle", () => {
    render_();
    const btn = screen.getByText("mediaPreview.preview");
    fireEvent.click(btn);
    expect(document.querySelector(".media-preview-content")).toBeTruthy();
    fireEvent.click(screen.getByText("mediaPreview.collapse"));
    expect(document.querySelector(".media-preview-content")).toBeNull();
  });

  it("expand button stops propagation", () => {
    const outer = vi.fn();
    const { container } = render(
      <div onClick={outer}>
        <MediaPreviewInline path="x.mp4" mediaType="video" />,
      </div>,
    );
    fireEvent.click(container.querySelector(".media-preview-expand")!);
    expect(outer).not.toHaveBeenCalled();
  });

  it("name click fires onFileClick(path) and stops propagation", () => {
    const onFileClick = vi.fn();
    const outer = vi.fn();
    const { container } = render(
      <div onClick={outer}>
        <MediaPreviewInline path="dir/clip.mp4" mediaType="video" onFileClick={onFileClick} />
      </div>,
    );
    fireEvent.click(container.querySelector(".media-preview-name")!);
    expect(onFileClick).toHaveBeenCalledWith("dir/clip.mp4");
    expect(outer).not.toHaveBeenCalled();
  });

  it("Enter and Space on the name fire onFileClick and prevent default", () => {
    const onFileClick = vi.fn();
    render_({ path: "clip.mp4", mediaType: "video", onFileClick });
    const nameEl = screen.getByText("clip.mp4");
    const enter = new KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true });
    Object.defineProperty(enter, "preventDefault", { value: vi.fn() });
    fireEvent(nameEl, enter);
    expect(onFileClick).toHaveBeenCalledWith("clip.mp4");
    expect((enter.preventDefault as ReturnType<typeof vi.fn>)).toHaveBeenCalled();

    const space = new KeyboardEvent("keydown", { key: " ", bubbles: true, cancelable: true });
    Object.defineProperty(space, "preventDefault", { value: vi.fn() });
    fireEvent(nameEl, space);
    expect(onFileClick).toHaveBeenCalledTimes(2);
    expect((space.preventDefault as ReturnType<typeof vi.fn>)).toHaveBeenCalled();
  });

  it("ignores other keys on the name", () => {
    const onFileClick = vi.fn();
    render_({ path: "clip.mp4", mediaType: "video", onFileClick });
    fireEvent.keyDown(screen.getByText("clip.mp4"), { key: "ArrowDown" });
    expect(onFileClick).not.toHaveBeenCalled();
  });

  it("does not throw when onFileClick is omitted", () => {
    render(<MediaPreviewInline path="clip.mp4" mediaType="video" />);
    expect(() => fireEvent.click(screen.getByText("clip.mp4"))).not.toThrow();
  });
});

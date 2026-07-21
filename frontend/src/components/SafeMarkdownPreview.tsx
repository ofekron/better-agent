import MarkdownPreview, {
  type MarkdownPreviewProps,
} from "@uiw/react-markdown-preview/nohighlight";
import "@uiw/react-markdown-preview/markdown.css";
import rehypeAttrs from "rehype-attr";
import rehypeHighlight from "rehype-highlight";

type SafeMarkdownPreviewProps = Omit<
  MarkdownPreviewProps,
  "pluginsFilter" | "rehypePlugins" | "skipHtml"
>;

const filterUnsafePlugins: NonNullable<MarkdownPreviewProps["pluginsFilter"]> = (
  type,
  plugins,
) => {
  if (type !== "rehype") return plugins;

  const safePlugins = plugins.filter((plugin) => {
    const pluginFunction = Array.isArray(plugin) ? plugin[0] : plugin;
    return pluginFunction !== rehypeAttrs;
  });
  if (plugins.length - safePlugins.length !== 1) {
    throw new Error("Safe markdown renderer could not isolate rehype-attr");
  }
  return safePlugins;
};

export function SafeMarkdownPreview(props: SafeMarkdownPreviewProps) {
  return (
    <MarkdownPreview
      {...props}
      skipHtml
      rehypePlugins={[rehypeHighlight]}
      pluginsFilter={filterUnsafePlugins}
    />
  );
}

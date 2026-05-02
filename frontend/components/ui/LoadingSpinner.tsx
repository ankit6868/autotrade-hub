export default function LoadingSpinner({ text }: { text?: string }) {
  return (
    <div className="flex flex-col items-center justify-center py-12">
      <div className="w-10 h-10 border-4 border-brand-600 border-t-transparent rounded-full animate-spin" />
      {text && <p className="mt-4 text-slate-400 text-sm">{text}</p>}
    </div>
  );
}

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { useTheme } from "@/hooks/use-theme";
import type { ThemePreference } from "@/lib/theme";

/** Appearance (client-only): the colour theme, saved in this browser. */
export function AppearanceSection() {
  const { preference, systemDark, setPreference } = useTheme();
  const options: { value: ThemePreference; label: string }[] = [
    { value: "light", label: "Light" },
    { value: "dark", label: "Dark" },
    { value: "system", label: `Match my system (currently ${systemDark ? "Dark" : "Light"})` },
  ];
  return (
    <Card>
      <CardHeader>
        <CardTitle>Appearance</CardTitle>
        <CardDescription>Saved in this browser only.</CardDescription>
      </CardHeader>
      <CardContent>
        <fieldset className="flex flex-col gap-2">
          <legend className="sr-only">Colour theme</legend>
          {options.map((option) => (
            <label key={option.value} className="flex items-center gap-2 text-sm">
              <input
                type="radio"
                name="theme-preference"
                value={option.value}
                checked={preference === option.value}
                onChange={() => setPreference(option.value)}
              />
              {option.label}
            </label>
          ))}
        </fieldset>
      </CardContent>
    </Card>
  );
}

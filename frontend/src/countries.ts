/** ISO-3166 alpha-2 codes for the country pickers (signup, account, admin
 * pricing). Pricing rows use the same codes, with '*' meaning "default". */
export const COUNTRIES: [string, string][] = [
  ['US', 'United States'], ['CA', 'Canada'], ['GB', 'United Kingdom'], ['IE', 'Ireland'],
  ['AU', 'Australia'], ['NZ', 'New Zealand'], ['DE', 'Germany'], ['FR', 'France'],
  ['ES', 'Spain'], ['PT', 'Portugal'], ['IT', 'Italy'], ['NL', 'Netherlands'],
  ['BE', 'Belgium'], ['CH', 'Switzerland'], ['AT', 'Austria'], ['SE', 'Sweden'],
  ['NO', 'Norway'], ['DK', 'Denmark'], ['FI', 'Finland'], ['PL', 'Poland'],
  ['CZ', 'Czechia'], ['GR', 'Greece'], ['RO', 'Romania'], ['HU', 'Hungary'],
  ['MX', 'Mexico'], ['BR', 'Brazil'], ['AR', 'Argentina'], ['CL', 'Chile'],
  ['CO', 'Colombia'], ['PE', 'Peru'], ['DO', 'Dominican Republic'], ['PR', 'Puerto Rico'],
  ['JP', 'Japan'], ['KR', 'South Korea'], ['SG', 'Singapore'], ['IN', 'India'],
  ['PH', 'Philippines'], ['ID', 'Indonesia'], ['MY', 'Malaysia'], ['TH', 'Thailand'],
  ['ZA', 'South Africa'], ['NG', 'Nigeria'], ['KE', 'Kenya'], ['EG', 'Egypt'],
  ['IL', 'Israel'], ['AE', 'United Arab Emirates'], ['SA', 'Saudi Arabia'], ['TR', 'Türkiye'],
];

export function countryName(code: string | null | undefined): string {
  if (!code || code === '*') return 'Default';
  return COUNTRIES.find(([c]) => c === code)?.[1] ?? code;
}

// R-02 — extraction enabled but no CDC annotation. Full load only, no delta.
@EndUserText.label: 'Material list, extraction only'
@Analytics.dataCategory: #DIMENSION
@Analytics.dataExtraction.enabled: true
define view entity ZI_NO_CDC
  as select from mara
{
  key matnr as Material,
      mtart as MaterialType,
      meins as BaseUnit
}

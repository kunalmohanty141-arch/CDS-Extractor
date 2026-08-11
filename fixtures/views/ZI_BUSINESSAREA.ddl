// Clean single-table view. SAP's own example of the automatic CDC path
// (Appendix A.2: I_BusinessArea on table TGSB).
@EndUserText.label: 'Business area extraction'
@Analytics: { dataCategory: #DIMENSION,
  dataExtraction: { enabled: true,
    delta.changeDataCapture.automatic: true } }
define view entity ZI_BUSINESSAREA
  as select from tgsb
{
  key gsber as BusinessArea,
      gtext as BusinessAreaName
}

import unittest
from pm3gui.hf_forms import parameters,form_arguments,action_title,COMMANDS


class HFFormTests(unittest.TestCase):
    def test_required_block_and_write_data_are_validated_before_execution(self):
        with self.assertRaisesRegex(ValueError,'Блок'):form_arguments('hf mf wrbl',{'data':'00'*16})
        with self.assertRaisesRegex(ValueError,'Данные'):form_arguments('hf mf wrbl',{'blk':'1'})
        args=form_arguments('hf mf wrbl',{'blk':'001','data':'aa '*16,'key':'ff '*6,'b':True})
        self.assertIn('--blk 1',args);self.assertIn('--data '+'AA'*16,args)
        self.assertIn('--key FFFFFFFFFFFF',args);self.assertIn('-b',args)

    def test_hex_integer_and_enum_reject_invalid_values(self):
        for values in ({'blk':'x','data':'00'*16},{'blk':'1','data':'AG'*16},{'blk':'1','data':'0'}):
            with self.assertRaises(ValueError):form_arguments('hf mf wrbl',values)
        with self.assertRaises(ValueError):form_arguments('hf mfdes write',{'algo':'unlisted'})

    def test_native_files_are_quoted_and_cannot_create_command_chains(self):
        args=form_arguments('hf mf restore',{'file':'C:/Library with spaces/card.bin'})
        self.assertIn('--file "C:/Library with spaces/card.bin"',args)
        for value in ('file; hf mf wipe','bad"name','a\nb'):
            with self.assertRaises(ValueError):form_arguments('hf mf restore',{'file':value})

    def test_repeated_keys_and_positional_data_preserve_single_command_semantics(self):
        params=parameters('hf mf chk')
        self.assertTrue(next(p for p in params if p.name=='key').repeated)
        args=form_arguments('hf mf chk',{'key':['FF '*6,'A0A1A2A3A4A5']})
        self.assertEqual(args.count('--key'),2)
        positional=next(p for p in parameters('hf 14a raw') if not p.flag)
        self.assertIn('3000',form_arguments('hf 14a raw',{positional.name:['30 00']}))

    def test_catalogue_has_consistent_fields_and_no_duplicate_parameter_names(self):
        available=0
        for command in COMMANDS:
            fields=parameters(command)
            self.assertEqual(len({p.name for p in fields}),len(fields),command)
            self.assertTrue(action_title(command))
            available+=bool(fields)
        self.assertGreater(available,450)

    def test_raw_radio_flags_are_not_mislabeled_as_mifare_keys(self):
        fields={p.name:p for p in parameters('hf 14a raw')}
        self.assertIn('CRC',fields['c'].label)
        self.assertIn('поле',fields['a'].label)
        self.assertNotEqual(fields['b'].label,'Ключ B')


if __name__=='__main__':unittest.main()
